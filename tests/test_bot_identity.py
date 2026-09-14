"""The bot says what it is: CTCP VERSION, and the list's own masthead.

Two surfaces, one constant. Before this there was no project URL anywhere in
the tree - no `github.com/...` in any .py or .md - so a list that reached a
stranger carried nothing saying what produced it, and a CTCP VERSION query got
silence.

Both of these answer PRIVATELY. The CTCP reply is a notice straight back to
whoever asked; the list header travels inside a file that somebody requested.
Neither puts a line in a channel, which is the distinction that ruled out a
`!version` command: the objection was never to the information, it was to
answering in public every time anyone asks.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import commands  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402
import list as list_mod  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TheVersionReplyGoesBackToWhoeverAsked(DCCoreTestCase):
    """ctcp_version_reply() - pulled out of irc_loop() so it is testable
    without driving the read loop, the same reason rehash_nick_change_line()
    is a standalone function."""

    def setUp(self):
        super().setUp()
        self.set_config(SCRIPT_VERSION="DCCore v9.9.9",
                        PROJECT_URL="https://github.com/Ninja-FSE/dccore",
                        CTCP_VERSION_REPLY=True)

    def test_it_is_a_notice_and_not_a_privmsg(self):
        """The whole reason this is safe. Two bots that both answer CTCP with
        a PRIVMSG answer each other forever; a NOTICE reply is the spec's way
        of breaking that loop. It is also why nothing reaches a channel."""
        reply = irc.ctcp_version_reply("curious")

        self.assertTrue(reply.startswith("NOTICE curious :"), reply)
        self.assertNotIn("PRIVMSG", reply)

    def test_it_is_wrapped_as_a_ctcp_reply(self):
        reply = irc.ctcp_version_reply("curious")
        body = reply.split(":", 1)[1]

        self.assertTrue(body.startswith("\x01VERSION "), repr(body))
        self.assertTrue(body.rstrip("\r\n").endswith("\x01"), repr(body))

    def test_it_carries_both_the_version_and_the_url(self):
        reply = irc.ctcp_version_reply("curious")

        self.assertIn("DCCore v9.9.9", reply)
        self.assertIn("https://github.com/Ninja-FSE/dccore", reply)

    def test_it_ends_with_a_terminated_irc_line(self):
        self.assertTrue(irc.ctcp_version_reply("curious").endswith("\r\n"))

    def test_disabled_means_silence_not_an_evasive_answer(self):
        """None rather than "": the caller must send nothing at all and be as
        quiet as the bot was before this feature existed."""
        self.set_config(CTCP_VERSION_REPLY=False)

        self.assertIsNone(irc.ctcp_version_reply("curious"))

    def test_a_blank_url_leaves_no_dangling_separator(self):
        """An operator who blanks one half should get a shorter reply, never
        a trailing " - " with nothing after it."""
        self.set_config(PROJECT_URL="")
        reply = irc.ctcp_version_reply("curious")

        self.assertIn("DCCore v9.9.9", reply)
        self.assertNotIn(" - \x01", reply)
        self.assertNotIn("- \x01", reply)

    def test_a_blank_version_leaves_no_leading_separator(self):
        self.set_config(SCRIPT_VERSION="")
        reply = irc.ctcp_version_reply("curious")

        self.assertIn("https://github.com/Ninja-FSE/dccore", reply)
        self.assertNotIn("VERSION - ", reply)

    def test_both_blank_still_names_the_project(self):
        """Control: the reply must never be an empty CTCP, which reads as a
        malformed answer rather than as a bot with nothing to say."""
        self.set_config(SCRIPT_VERSION="", PROJECT_URL="")
        reply = irc.ctcp_version_reply("curious")

        self.assertIn("DCCore", reply)
        self.assertNotIn("VERSION \x01", reply)


class TheVersionQueryIsFloodGated(unittest.TestCase):
    """An unthrottled CTCP responder is a standard way to make a bot flood
    ITSELF off the network: a few hundred queries and the bot's own replies
    trip the server's excess-flood.

    Read out of irc.py rather than retyped, matching test_irc_dispatch.py's
    reasoning - a test that restates the condition agrees with itself and not
    with the daemon.
    """

    def test_version_is_in_the_flood_gated_ctcp_set(self):
        source_path = os.path.join(REPO_ROOT, "irc.py")
        with io.open(source_path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()

        gated = [line for line in lines
                 if 'strip("\\x01").strip().upper() in (' in line]

        self.assertTrue(
            gated,
            "the CTCP set feeding is_bot_command was not found - this scan is "
            "looking at the wrong shape, not at a passing condition")

        for line in gated:
            self.assertIn(
                "VERSION", line,
                "CTCP VERSION must sit in the same flood-gated set as QUE and "
                "REMOVE, or a flood of VERSION queries makes the bot answer "
                "itself off the network")


class TheIdentityLine(DCCoreTestCase):
    """list_identity_line() - shared by the .txt and the !rar list so the two
    cannot drift apart."""

    def setUp(self):
        super().setUp()
        self.set_config(NICKNAME="SomeBot",
                        SCRIPT_VERSION="DCCore v9.9.9",
                        PROJECT_URL="https://github.com/Ninja-FSE/dccore")

    def test_it_names_the_bot_the_build_and_the_project(self):
        line = update_list.list_identity_line()

        self.assertIn("SomeBot", line)
        self.assertIn("DCCore v9.9.9", line)
        self.assertIn("https://github.com/Ninja-FSE/dccore", line)

    def test_a_blank_url_leaves_no_dangling_separator(self):
        self.set_config(PROJECT_URL="")
        line = update_list.list_identity_line()

        self.assertTrue(line.endswith("DCCore v9.9.9"), line)

    def test_both_blank_still_names_the_bot(self):
        """The nickname is the part a recipient actually needs - it is who to
        ask for a file. It must survive both other parts being blank."""
        self.set_config(SCRIPT_VERSION="", PROJECT_URL="")
        line = update_list.list_identity_line()

        self.assertEqual(line, "Served by SomeBot")
        self.assertNotIn(" - ", line)

    # A fallback here must be falsy rather than a literal like "DCCore": a
    # non-empty one is a second opinion about a value config.py already
    # declares, so which wins depends on how the value happened to be read.
    # This function is where that was got wrong first time round.
    #
    # NOT asserted here. tests/test_config_fallbacks.py already enforces it
    # across every module by parsing the AST, and the version of it that lived
    # here scanned the source text - which matched the word "DCCore" in this
    # very comment's predecessor in the docstring and failed on a function that
    # was correct. A guard that cannot tell a mention from the thing is worse
    # than no guard, and duplicating a working one badly is how you get one.


class TheOperatorBanner(DCCoreTestCase):
    """read_operator_header() - free-form text the operator puts at the top of
    every generated list."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.path = os.path.join(self.tree.root, "list_header.txt")
        self.set_config(LIST_HEADER_FILE=self.path,
                        LIST_HEADER_MAX_BYTES=8192)

    def write(self, data):
        with io.open(self.path, "wb") as handle:
            handle.write(data)

    def test_a_missing_file_is_the_normal_case_not_an_error(self):
        """Most installs will never create one. A banner is decoration, and no
        decoration should ever stop a list being built."""
        self.assertEqual(update_list.read_operator_header(), "")

    def test_an_empty_file_gives_nothing(self):
        self.write(b"")

        self.assertEqual(update_list.read_operator_header(), "")

    def test_ascii_art_survives_verbatim(self):
        """The point of the feature. Everything else written into the list goes
        through _one_line(), which flattens control characters; running the
        banner through it would destroy the art this exists to carry."""
        art = ("  ___   ___ \n"
               " |   \\ / __|\n"
               " | |) | (__ \n"
               " |___/ \\___|\n"
               "   #somechannel")
        self.write(art.encode("utf-8"))

        self.assertEqual(update_list.read_operator_header(), art)

    def test_crlf_does_not_become_a_double_carriage_return(self):
        """The list is opened without newline="", so the writer translates
        "\\n" to os.linesep itself. A CRLF banner passed through untouched
        comes out "\\r\\r\\n" on Windows."""
        self.write(b"line one\r\nline two\r\n")
        result = update_list.read_operator_header()

        self.assertNotIn("\r", result)
        self.assertEqual(result, "line one\nline two")

    def test_trailing_blank_lines_are_dropped(self):
        """So this function owns the spacing around the banner rather than
        inheriting whatever the operator's editor left behind."""
        self.write(b"banner\n\n\n\n")

        self.assertEqual(update_list.read_operator_header(), "banner")

    def test_it_truncates_past_the_cap(self):
        """Someone will eventually point this at the wrong file, and without a
        cap that staples megabytes onto every list request."""
        self.set_config(LIST_HEADER_MAX_BYTES=64)
        self.write(b"x" * 5000)
        result = update_list.read_operator_header()

        self.assertEqual(len(result), 64)

    def test_a_file_just_under_the_cap_is_not_truncated(self):
        """Control for the boundary: the cap must not trim a legitimate
        banner that happens to sit near it."""
        self.set_config(LIST_HEADER_MAX_BYTES=64)
        self.write(b"y" * 64)

        self.assertEqual(len(update_list.read_operator_header()), 64)

    def test_an_unreadable_path_is_not_an_error(self):
        """A directory where a file was expected - a plausible typo - must
        read as "no banner", not raise into the middle of a list build."""
        self.set_config(LIST_HEADER_FILE=self.tree.root)

        self.assertEqual(update_list.read_operator_header(), "")

    def test_undecodable_bytes_do_not_abort_the_build(self):
        """Operator-supplied, so it may be in any code page."""
        self.write(b"caf\xe9 latte")

        self.assertIn("caf", update_list.read_operator_header())


class TheGeneratedListCarriesItsIdentity(DCCoreTestCase):
    """The end-to-end property: a real list, generated, and what its first
    lines say."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.header_path = os.path.join(self.tree.root, "list_header.txt")
        self.set_config(LOCAL_LIST_DIR=self.tree.lists,
                        FILE_DIRECTORY=self.tree.music,
                        LIST_BASE_NAME="DCCore",
                        NICKNAME="DCCore",
                        ORIGINAL_NICK="DCCore",
                        SCRIPT_VERSION="DCCore v9.9.9",
                        PROJECT_URL="https://github.com/Ninja-FSE/dccore",
                        LIST_HEADER_FILE=self.header_path,
                        LIST_HEADER_MAX_BYTES=8192)
        self.add("Artist/Album/track.flac")

    def add(self, relative, data=b"\x00" * 2048):
        path = os.path.join(self.tree.music, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "wb") as handle:
            handle.write(data)

    def build(self):
        update_list.generate_master_list()
        import list as list_mod
        index = list_mod.find_latest_list()
        self.assertIsNotNone(index, "no list was produced")
        with io.open(index, encoding="utf-8") as handle:
            return handle.read()

    def test_the_identity_line_is_present(self):
        body = self.build()

        self.assertIn("DCCore v9.9.9", body)
        self.assertIn("https://github.com/Ninja-FSE/dccore", body)

    def test_the_file_count_still_reads_with_a_banner_above_the_folders(self):
        """THE regression guard for this feature.

        commands.count_from_master_list() does one readline() and regexes
        "List of N Files" out of it. Anything inserted ABOVE that line stops
        the regex matching, and it fails silently - no exception, no empty
        file, just a count of zero feeding !update and the channel advert.

        So this asserts the count survives, not merely that the banner is
        somewhere in the file: the placement is the whole design decision.
        """
        with io.open(self.header_path, "wb") as handle:
            handle.write(b"=== WELCOME ===\nfive lines\nof\nascii\nart")

        body = self.build()
        first_line = body.splitlines()[0]

        self.assertIn("=== WELCOME ===", body)
        self.assertRegex(first_line, r"List of\s+[\d,]+\s+Files")

        # Against the header's own number rather than a hardcoded one: what
        # matters is that the counter and the file agree, and a literal here
        # would only be asserting how many files the fixture happens to make.
        stated = int(re.search(r"List of\s+([\d,]+)\s+Files",
                               first_line).group(1).replace(",", ""))

        self.assertGreater(stated, 0, "the fixture produced an empty list")
        self.assertEqual(commands.count_from_master_list(), stated)

    def test_the_count_is_identical_with_and_without_the_banner(self):
        """Control, and the sharper form of the guard above: the banner must
        make no difference at all to what the counter reads. If placement ever
        moves back above line 1 these two diverge, which a single-case test
        with a hardcoded expectation could miss."""
        self.build()
        without = commands.count_from_master_list()

        with io.open(self.header_path, "wb") as handle:
            handle.write(b"=== WELCOME ===\nfive lines\nof\nascii\nart")
        self.build()
        with_banner = commands.count_from_master_list()

        self.assertGreater(without, 0, "the fixture produced an empty list")
        self.assertEqual(without, with_banner)

    def test_the_rar_list_carries_the_same_masthead(self):
        """The !rar album list is a separate download that travels on its own,
        so it needs its own copy rather than inheriting the .txt's."""
        self.set_config(RAR_ENABLED=True)
        with io.open(self.header_path, "wb") as handle:
            handle.write(b"MARKER-BANNER")

        self.build()

        rar_lists = [name for name in os.listdir(self.tree.lists)
                     if "-RAR-" in name and name.endswith(".txt")]
        self.assertTrue(rar_lists, "no !rar list was produced")

        with io.open(os.path.join(self.tree.lists, rar_lists[0]),
                     encoding="utf-8") as handle:
            rar_body = handle.read()

        self.assertIn("MARKER-BANNER", rar_body)
        self.assertIn("DCCore v9.9.9", rar_body)
        self.assertIn("https://github.com/Ninja-FSE/dccore", rar_body)

    def test_the_rar_list_is_not_counted_as_the_master_list(self):
        """Control for the above: adding a header to the !rar list must not
        make it look like the master index. Its own first line carries no
        number, which is why count_from_master_list()'s regex never matched
        it - and that has to stay true now that it has a masthead too."""
        self.set_config(RAR_ENABLED=True)
        self.build()

        rar_lists = [name for name in os.listdir(self.tree.lists)
                     if "-RAR-" in name and name.endswith(".txt")]
        with io.open(os.path.join(self.tree.lists, rar_lists[0]),
                     encoding="utf-8") as handle:
            first_line = handle.readline()

        self.assertNotRegex(first_line, r"List of\s+[\d,]+\s+Files")

    def test_the_banner_sits_between_the_masthead_and_the_first_folder(self):
        """Where the operator asked for it: below the identity line, above the
        listing."""
        with io.open(self.header_path, "wb") as handle:
            handle.write(b"MARKER-BANNER")

        lines = self.build().splitlines()
        identity_at = next(n for n, l in enumerate(lines) if "Served by" in l)
        banner_at = next(n for n, l in enumerate(lines) if "MARKER-BANNER" in l)
        folder_at = next(n for n, l in enumerate(lines) if "Artist" in l)

        self.assertGreater(identity_at, 0, "the masthead must not be line 1")
        self.assertLess(identity_at, banner_at,
                        "the masthead must come above the operator's banner")
        self.assertLess(banner_at, folder_at,
                        "the banner must come before the first folder")

    def test_a_huge_banner_cannot_push_the_masthead_off_the_screen(self):
        """The reason the masthead goes above the banner rather than below it.

        The banner is free-form and can be any height, so ordering them the
        other way would bury the attribution exactly on the installs that
        decorate the most - the reader opens the file, sees two hundred lines
        of ASCII art, and never scrolls to the line saying which bot this is.

        Asserted as "within the first few lines whatever the banner does",
        which is the property, rather than as a fixed line number, which would
        pass while breaking the moment a header line is added.
        """
        with io.open(self.header_path, "wb") as handle:
            handle.write(b"\n".join(b"decorative line %d" % n
                                    for n in range(200)))

        lines = self.build().splitlines()
        identity_at = next(n for n, l in enumerate(lines) if "Served by" in l)

        self.assertLess(
            identity_at, 5,
            "a 200-line banner pushed the masthead to line "
            f"{identity_at}; it must stay on the reader's first screen")


class TheDeliveredCopyShowsTheBannerOnce(DCCoreTestCase):
    """The `-FULL-` text download is the two lists concatenated, and each of
    them carries its own banner - correct when they are handed out separately
    (.zip and .rar do, and the !rar list is served on its own), wrong once
    they are joined.

    The operator's ASCII art appearing again halfway down the file they
    downloaded reads as a bug rather than as a design, and the banner can be
    any height.
    """

    BANNER = b"MARKER-BANNER-LINE\nsecond art line"

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.header_path = os.path.join(self.tree.root, "list_header.txt")
        with io.open(self.header_path, "wb") as handle:
            handle.write(self.BANNER)
        self.set_config(LOCAL_LIST_DIR=self.tree.lists,
                        FILE_DIRECTORY=self.tree.music,
                        LIST_BASE_NAME="Bot", NICKNAME="Bot",
                        ORIGINAL_NICK="Bot", LIST_FORMAT="txt",
                        RAR_ENABLED=True,
                        SCRIPT_VERSION="DCCore v9.9.9",
                        PROJECT_URL="https://github.com/Ninja-FSE/dccore",
                        LIST_HEADER_FILE=self.header_path,
                        LIST_HEADER_MAX_BYTES=8192)
        path = os.path.join(self.tree.music, "Artist", "Album", "track.flac")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "wb") as handle:
            handle.write(b"\x00" * 2048)
        update_list.generate_master_list()

    def read(self, needle):
        name = next(n for n in os.listdir(self.tree.lists)
                    if needle in n and n.endswith(".txt"))
        with io.open(os.path.join(self.tree.lists, name),
                     encoding="utf-8") as handle:
            return handle.read()

    def test_the_delivered_copy_carries_the_banner_once(self):
        body = self.read(list_mod.FULL_LIST_MARKER)

        self.assertEqual(body.count("MARKER-BANNER-LINE"), 1)
        self.assertEqual(body.count("second art line"), 1)

    def test_the_standalone_rar_list_keeps_its_own_banner(self):
        """Control, and the reason this is done at concatenation rather than
        by not writing it: the !rar list is also handed out on its own, and
        stripping it at the writer would leave that copy unbranded."""
        body = self.read("-RAR-")

        self.assertEqual(body.count("MARKER-BANNER-LINE"), 1)

    def test_the_master_index_keeps_its_banner(self):
        """Control on the other member."""
        body = self.read("Bot-2026")

        self.assertGreaterEqual(body.count("MARKER-BANNER-LINE"), 1)

    def test_the_identity_line_still_repeats_per_section(self):
        """Deliberate, and the distinction worth pinning: the album half of
        the delivered file should still say what is serving it. One line is a
        section header; a banner of arbitrary height is not."""
        body = self.read(list_mod.FULL_LIST_MARKER)

        self.assertEqual(body.count("Served by Bot"), 2)

    def test_both_sections_are_still_present(self):
        """Control. Removing the banner must not have removed anything else -
        a concatenation that lost its album half would satisfy the count
        assertions above perfectly."""
        body = self.read(list_mod.FULL_LIST_MARKER)

        self.assertIn("List of", body)
        self.assertIn("List of Entire Album Folders", body)
        self.assertIn("!Bot !rar", body)
        self.assertIn("track.flac", body)


class NothingInTheMastheadLooksLikeACommand(DCCoreTestCase):
    """The list is read by scripts, not only by people.

    AutoQ.mrc copies request lines out of it and sends them verbatim, so a
    header line that happens to begin with the request trigger would be sent
    to the bot as a request for a file that does not exist. The masthead added
    lines to this file, which is what makes it worth pinning rather than
    assuming.
    """

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.header_path = os.path.join(self.tree.root, "list_header.txt")
        self.set_config(LOCAL_LIST_DIR=self.tree.lists,
                        FILE_DIRECTORY=self.tree.music,
                        LIST_BASE_NAME="DCCore", NICKNAME="DCCore",
                        ORIGINAL_NICK="DCCore",
                        SCRIPT_VERSION="DCCore v9.9.9",
                        PROJECT_URL="https://github.com/Ninja-FSE/dccore",
                        LIST_HEADER_FILE=self.header_path,
                        LIST_HEADER_MAX_BYTES=8192)
        path = os.path.join(self.tree.music, "Artist", "Album", "track.flac")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with io.open(path, "wb") as handle:
            handle.write(b"\x00" * 2048)

    def lists(self):
        """(txt lines, rar lines) from a real generated pair."""
        with io.open(self.header_path, "wb") as handle:
            handle.write(b"  ___  \n |   | decorative art, no trigger here\n")
        self.set_config(RAR_ENABLED=True)
        update_list.generate_master_list()

        import list as list_mod
        with io.open(list_mod.find_latest_list(), encoding="utf-8") as handle:
            txt = handle.read().splitlines()

        rar_name = next(n for n in os.listdir(self.tree.lists)
                        if "-RAR-" in n and n.endswith(".txt"))
        with io.open(os.path.join(self.tree.lists, rar_name),
                     encoding="utf-8") as handle:
            rar = handle.read().splitlines()
        return txt, rar

    def test_no_line_above_the_listing_starts_with_the_request_trigger(self):
        trigger = f"!{config.NICKNAME}"
        txt, _rar = self.lists()

        first_request = next(n for n, l in enumerate(txt)
                             if l.startswith(trigger))

        for index, line in enumerate(txt[:first_request]):
            self.assertFalse(
                line.startswith(trigger),
                f"list line {index} is part of the header but begins with the "
                f"request trigger, so a script reading this file would send it "
                f"back as a request: {line!r}")

    def test_the_rar_masthead_does_not_shadow_a_rar_request(self):
        """Same property on the !rar list, where the request line is the whole
        command and the masthead sits directly above it."""
        trigger = f"!{config.NICKNAME}"
        _txt, rar = self.lists()

        first_request = next(n for n, l in enumerate(rar)
                             if l.startswith(trigger))

        for line in rar[:first_request]:
            self.assertFalse(line.startswith(trigger), line)

    def test_the_request_rows_themselves_are_untouched(self):
        """Control. The check above would pass trivially on a list that had
        stopped emitting request rows at all, which is the failure it is least
        able to notice on its own."""
        trigger = f"!{config.NICKNAME}"
        txt, rar = self.lists()

        file_rows = [l for l in txt if l.startswith(trigger)]
        rar_rows = [l for l in rar if l.startswith(trigger)]

        self.assertTrue(file_rows, "the .txt list emitted no request rows")
        self.assertTrue(rar_rows, "the !rar list emitted no request rows")
        self.assertIn("track.flac", " ".join(file_rows))
        self.assertTrue(any(" !rar " in row for row in rar_rows))

    def test_the_rar_request_row_carries_nothing_after_the_folder(self):
        """The constraint itself. AutoQ.mrc sends this line verbatim, so a
        size or any other trailing field stops it matching — see the comment
        at the write site in update_list.py and #69."""
        trigger = f"!{config.NICKNAME} !rar "
        _txt, rar = self.lists()

        rows = [l for l in rar if l.startswith(trigger)]
        self.assertTrue(rows, "no !rar request row was produced")

        for row in rows:
            folder = row[len(trigger):]
            self.assertTrue(
                folder.endswith("\\"),
                f"a !rar row must end at the folder it requests, with nothing "
                f"appended: {row!r}")


if __name__ == "__main__":
    unittest.main()
