"""A folder is offered as .rar because that bot's list says so.

WHAT IT REPLACED. "Get folder as .rar" sat on every folder heading of every
fetched list, gated only on "this is not our own list". That could never be
right: it is a claim about what a FOREIGN bot will do, made from nothing.

Measured against one live registry: 2 of 51 known bots publish a RAR folder
list at all. So the button was wrong for the other 49, and a click on one sent
"!<nick> !rar <folder>" into the channel and then held one of MAX_FETCH_SLOTS
for FETCH_FOLDER_OFFER_TIMEOUT - half an hour - waiting for a reply that was
never coming. Three clicks and cross-bot fetching was dead for the afternoon.

AND PER-BOT WOULD STILL HAVE BEEN WRONG. From the operator: "rar file list can
be different than normal filelist. i can offer 1 folder as normal file list
and 1 other folder as rar filelist only." Our own update_list.py does exactly
that - a folder earns its !rar row from holding a PACKABLE file, so the two
lists are different sets even here.

WHAT THE LIST ALREADY SAYS. A bot that packs albums publishes a separate list
whose every row is the line to type:

    !SomeBot !rar D:\\MUSIC\\Amon Amarth - 1999 - The Avenger\\

That is a per-folder statement of what that bot will pack, published by the
bot itself. Nothing has to be inferred from an advert, and nothing has to be
guessed at from a filename convention that only our own lists follow. The
button goes on the row that carries the request line, and the folder it sends
is the one that row asks for.

The title is left exactly as the list wrote it. Those rows exist to be copied
verbatim - it is what the header of every such list tells the reader to do -
so this adds a field beside the title rather than reformatting it.
"""

import io
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import list as list_mod  # noqa: E402

BACKSLASH = chr(92)
NEWLINE = chr(10)


def app_js():
    with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                 encoding="utf-8") as handle:
        return handle.read()


class ReadingTheRequestLine(unittest.TestCase):

    def test_a_pack_request_yields_its_folder(self):
        folder = "D:" + BACKSLASH + "MUSIC" + BACKSLASH + "Some Album"

        self.assertEqual(list_mod.rar_folder_of("!rar " + folder), folder)

    def test_an_ordinary_file_row_is_not_one(self):
        self.assertEqual(list_mod.rar_folder_of("Track01.flac"), "")

    def test_the_verb_is_matched_without_regard_to_case(self):
        self.assertEqual(list_mod.rar_folder_of("!RAR SomeFolder"), "SomeFolder")

    def test_a_bare_verb_asks_for_nothing(self):
        """Not a request for a folder named "". An empty answer is the honest
        reading, and it is what keeps the button off the row."""
        self.assertEqual(list_mod.rar_folder_of("!rar"), "")
        self.assertEqual(list_mod.rar_folder_of("!rar   "), "")

    def test_a_verb_inside_a_filename_is_not_a_request(self):
        """Anchored at the start. A track called "the !rar song.flac" is a
        file, and offering to pack it would send a line that means nothing."""
        self.assertEqual(list_mod.rar_folder_of("the !rar song.flac"), "")

    def test_the_verb_needs_a_space_after_it(self):
        """Found by a mutation run: with "\\s*" instead of "\\s+" the regex
        still matched, and a file whose name merely BEGINS "!rar" was read as
        a request for whatever followed - "!rarely used.flac" becoming a
        request to pack "ely used.flac"."""
        self.assertEqual(list_mod.rar_folder_of("!rarely used.flac"), "")
        self.assertEqual(list_mod.rar_folder_of("!rarities.zip"), "")

    def test_nothing_at_all(self):
        self.assertEqual(list_mod.rar_folder_of(""), "")
        self.assertEqual(list_mod.rar_folder_of(None), "")


class ARealRarListCarriesItThroughTheParse(unittest.TestCase):
    """End to end from the file on disk: this is the shape update_list.py
    writes, and the shape a peer's own packer writes."""

    def rows(self, body):
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".txt", delete=False, encoding="utf-8", newline="")
        handle.write(body)
        handle.close()
        self.addCleanup(os.unlink, handle.name)
        entries, _total = list_mod.find_matching_entries(
            [], limit=None, list_path=handle.name)
        return list_mod.entries_to_filelist_rows(entries, "SomeBot")

    def rar_list(self):
        return (
            "List of Entire Album Folders (!rar) for !SomeBot" + NEWLINE
            + "=" * 60 + NEWLINE + NEWLINE
            + "!SomeBot !rar D:" + BACKSLASH + "MUSIC" + BACKSLASH
            + "First Album" + BACKSLASH + NEWLINE
            + "!SomeBot !rar D:" + BACKSLASH + "MUSIC" + BACKSLASH
            + "Second Album" + BACKSLASH + NEWLINE)

    def test_every_row_of_a_rar_list_names_its_folder(self):
        rows = self.rows(self.rar_list())

        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["rar_folder"] for row in rows))

    def test_the_folder_is_the_one_the_line_asks_for(self):
        rows = self.rows(self.rar_list())

        self.assertEqual(
            rows[0]["rar_folder"],
            "D:" + BACKSLASH + "MUSIC" + BACKSLASH + "First Album" + BACKSLASH)

    def test_the_title_is_left_exactly_as_the_list_wrote_it(self):
        """These rows exist to be copied verbatim - the header of every such
        list says so - so the field is added BESIDE the title."""
        rows = self.rows(self.rar_list())

        self.assertTrue(rows[0]["title"].startswith("!rar "))

    def test_an_ordinary_list_offers_to_pack_nothing(self):
        body = ("List of Files" + NEWLINE + "=" * 60 + NEWLINE + NEWLINE
                + "!SomeBot Track01.flac  ::INFO:: 5000000" + NEWLINE
                + "!SomeBot Track02.flac  ::INFO:: 5000000" + NEWLINE)

        rows = self.rows(body)

        self.assertEqual(len(rows), 2)
        self.assertEqual([row["rar_folder"] for row in rows], ["", ""])

    def test_the_field_is_on_every_row_either_way(self):
        """One row shape for both surfaces. A key present on some rows and
        absent on others is how the frontend stops being able to trust it -
        the same reason "mark" is declared empty rather than added by
        whichever payload happens to know."""
        body = ("List" + NEWLINE + "=" * 60 + NEWLINE + NEWLINE
                + "!SomeBot Track01.flac  ::INFO:: 100" + NEWLINE)

        self.assertIn("rar_folder", self.rows(body)[0])


class ThePageOffersItOnTheRowThatAsks(unittest.TestCase):

    def heading(self):
        body = app_js().split("function folderHeadingHtml(", 1)[1]
        return body.split("\n    function ", 1)[0]

    def files(self):
        body = app_js().split("function folderFilesHtml(", 1)[1]
        return body.split("\n    function setFolderExpanded(", 1)[0]

    def test_the_heading_no_longer_offers_to_pack_a_folder(self):
        self.assertNotIn("folder-rar-btn", self.heading())

    def test_the_row_does(self):
        self.assertIn("folder-rar-btn", self.files())

    def test_only_a_row_that_carries_a_request_line(self):
        self.assertIn("row.rar_folder && fetchable", self.files())

    def test_it_shares_the_checkboxs_gate(self):
        """Packing a folder makes no sense against our own list, and the
        checkbox in the same function already decides exactly that."""
        self.assertIn("var fetchable =", self.files())

    def test_the_folder_sent_is_the_rows_own(self):
        """NOT the heading it sits under. A RAR list's rows are grouped under
        whatever heading that list carries, which is not the folder being
        asked for - sending the heading would request the wrong thing, or
        nothing."""
        body = app_js().split("function attachFilelistsFolderRarData(", 1)[1]
        body = body.split("\n    function ", 1)[0]

        self.assertIn("row.rar_folder", body)
        self.assertNotIn("dataset.folder = group.folder", body)

    def test_the_folder_reaches_the_dom_by_assignment(self):
        """It is a path out of a foreign bot's list, and escapeHtml() does not
        encode a double quote - so it must never be concatenated into markup.
        Same rule as the nick beside it."""
        body = app_js().split("function attachFilelistsFolderRarData(", 1)[1]
        body = body.split("\n    function ", 1)[0]

        self.assertIn(".dataset.folder =", body)
        for unsafe in ('data-folder="', "innerHTML", "insertAdjacentHTML"):
            self.assertNotIn(unsafe, body)

    def test_the_row_is_found_by_its_own_index(self):
        """"entryIndex", not "rowIndex": the folder picker has a guard
        refusing that word anywhere in this file, because it once stored a row
        index across add/remove/reorder while a panel was open. This index has
        a different lifetime - one render pass, the same array
        attachFilelistsFolderRarData() is handed - and weakening their guard
        to make room for it would be the wrong trade."""
        source = app_js()

        self.assertIn("data-entry-index=", source)
        self.assertNotIn("rowIndex", source)


if __name__ == "__main__":
    unittest.main()
