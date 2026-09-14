"""The filter bar searches what was typed, in the order it was typed.

ASKED FOR IN THE BETA:

    "well when i type amon a i want it to search 'amon a' only. if i type
     amon amar i want it to search 'amon amar'. if i want the 2nd word to be
     in any place then i search for amon*amar. also, it it possible to
     highlight the matched characters on the filtered list?"

WHAT IT REPLACED. Every word was ANDed and could appear anywhere, so a
two-word query was WIDER than a one-word query in every way that mattered -
the second word is usually short, and a short word is in half the library.
Typing an artist and the first letters of an album returned every track by
that artist whose title happened to contain a standalone "a".

    typed          old                        new
    amon a         amon AND a  (anywhere)     "amon a" as a phrase
    amon amar      amon AND amar (anywhere)   "amon amar" as a phrase
    amon*amar      - no meaning -             amon AND amar, anywhere

THE PREFIX RULE IS WHY IT WORKS. The last phrase's last word is still matched
as a prefix, because it is the word being typed - so "amon a" reaches "Amon
Amarth". The old length floor refusing a wildcard on one character is right
for a bare "a", which would be every row, and wrong inside a phrase, where
"amon" has already anchored it: without the exception "amon a" would ask for
the standalone word "a" straight after "amon", which is nothing anybody types
it for.

AND THE HIGHLIGHT marks what was TYPED, not what FTS5 matched. The last phrase
is a prefix, so "amar" matches "Amarth"; marking the four characters the
operator put in is the honest reading of "highlight the matched characters".
"""

import io
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import list_index  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

FOLDER = "D:" + chr(92) + "Music"


class ReadingWhatWasTyped(unittest.TestCase):

    def test_words_are_one_phrase(self):
        self.assertEqual(list_index.filter_segments("amon amar"), ["amon amar"])

    def test_a_star_separates_two(self):
        self.assertEqual(list_index.filter_segments("amon*amar"),
                         ["amon", "amar"])

    def test_a_star_can_separate_phrases_not_just_words(self):
        self.assertEqual(list_index.filter_segments("amon amar*live"),
                         ["amon amar", "live"])

    def test_spacing_around_the_star_does_not_matter(self):
        self.assertEqual(list_index.filter_segments("amon * amar"),
                         ["amon", "amar"])

    def test_a_trailing_star_is_not_an_empty_phrase(self):
        self.assertEqual(list_index.filter_segments("amon*"), ["amon"])
        self.assertEqual(list_index.filter_segments("*amon"), ["amon"])
        self.assertEqual(list_index.filter_segments("amon**amar"),
                         ["amon", "amar"])

    def test_nothing_typed_is_nothing_to_search(self):
        self.assertEqual(list_index.filter_segments(""), [])
        self.assertEqual(list_index.filter_segments("   "), [])
        self.assertEqual(list_index.filter_segments(None), [])


class BuildingTheQuery(unittest.TestCase):

    def query(self, typed):
        return list_index.build_match_query(list_index.filter_segments(typed))

    def test_a_phrase_is_quoted_as_one(self):
        self.assertEqual(self.query("amon amar"), 'filename:("amon amar"*)')

    def test_two_phrases_are_anded(self):
        self.assertEqual(self.query("amon*amar"),
                         'filename:("amon" AND "amar"*)')

    def test_only_the_last_phrase_gets_the_prefix(self):
        """The one being typed. The others are finished words."""
        self.assertEqual(self.query("amon amar*live"),
                         'filename:("amon amar" AND "live"*)')

    def test_a_lone_short_word_gets_no_wildcard(self):
        """"a"* is every row in the index, which is why the floor exists."""
        self.assertEqual(self.query("a"), 'filename:("a")')

    def test_but_a_short_word_inside_a_phrase_does(self):
        """"amon" has already anchored it, so the wildcard costs nothing -
        and without it "amon a" asks for the standalone word "a" straight
        after "amon", which is not what anybody types it for."""
        self.assertEqual(self.query("amon a"), 'filename:("amon a"*)')

    def test_nothing_typed_builds_nothing(self):
        self.assertIsNone(self.query(""))
        self.assertIsNone(self.query("*"))

    def test_the_operators_star_never_reaches_the_expression(self):
        """It is a separator here. Reaching FTS5 it would be syntax."""
        for typed in ("amon*amar", "amon**amar", "*amon*"):
            self.assertNotIn("*amar", self.query(typed) or "")


class AgainstARealIndex(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-phrase-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp,
                                                            ignore_errors=True))
        config.LIST_INDEX_FILE = os.path.join(self.tmp, "index.db")
        list_index.index_bot_list("somebot", [
            {"title": "Amon Amarth - The Avenger - 07 - Legend Of A Man.flac",
             "size": "1MB", "folder": FOLDER},
            {"title": "Amon Amarth - The Crusher - 02 - Masters Of War.flac",
             "size": "1MB", "folder": FOLDER},
            {"title": "Amorphis - Black Winter Day.flac",
             "size": "1MB", "folder": FOLDER},
            {"title": "Some Other - A Live Recording.flac",
             "size": "1MB", "folder": FOLDER},
        ])

    def found(self, typed):
        rows = list_index.search(list_index.filter_segments(typed), limit=50)
        return sorted(row["filename"] for row in rows)

    def test_the_reported_case(self):
        """"amon a" found every track with a standalone "a" in it. It finds
        the artist now."""
        found = self.found("amon a")

        self.assertEqual(len(found), 2)
        self.assertTrue(all(name.startswith("Amon Amarth") for name in found))

    def test_it_does_not_reach_a_similar_artist(self):
        """"Amorphis" begins with the same three letters and is not it."""
        self.assertNotIn("Amorphis - Black Winter Day.flac",
                         self.found("amon a"))

    def test_it_does_not_reach_an_unrelated_standalone_a(self):
        self.assertNotIn("Some Other - A Live Recording.flac",
                         self.found("amon a"))

    def test_typing_further_keeps_narrowing(self):
        self.assertEqual(self.found("amon amar"), self.found("amon a"))

    def test_a_star_puts_the_words_anywhere(self):
        found = self.found("amon*war")

        self.assertEqual(found,
                         ["Amon Amarth - The Crusher - 02 - Masters Of War.flac"])

    def test_a_lone_short_word_still_finds_what_holds_it(self):
        """And is not silently turned into a prefix that matches
        everything."""
        found = self.found("a")

        self.assertIn("Some Other - A Live Recording.flac", found)
        self.assertNotIn("Amorphis - Black Winter Day.flac", found)


class ThePageMarksWhatWasTyped(unittest.TestCase):

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                     encoding="utf-8") as handle:
            return handle.read()

    def body(self):
        return self.source().split("function highlightedTitle(", 1)[1] \
                            .split("\n    }", 1)[0]

    def test_the_title_goes_through_it(self):
        self.assertIn("highlightedTitle(row.title)", self.source())

    def test_it_uses_the_segments_the_server_matched_on(self):
        """Parsed once, on the server. Two parses of "amon*amar" could
        disagree about where the phrase boundary is."""
        self.assertIn("state.filelistsMatchTerms", self.body())

    def test_the_segments_come_from_the_payload(self):
        self.assertIn("payload.terms", self.source())

    def test_every_piece_is_escaped(self):
        """The title comes off another bot's list. Marking part of it must not
        turn the rest into markup, so every piece goes through the same
        escapeHtml() the whole title always had.

        Counted occurrences at first, which a mutation walked straight
        through: dropping the escape from ONE of five pieces still left four.
        The property is that no slice of the title reaches the output raw."""
        body = self.body()
        raw = [line.strip() for line in body.splitlines()
               if "text.slice(" in line and "escapeHtml(" not in line]

        self.assertEqual(raw, [],
                         "a piece of the title reaches the output unescaped")
        self.assertNotIn("innerHTML", body)

    def test_that_check_can_see_the_slices(self):
        """Guard on the guard: if the function stopped slicing, or the body
        came back empty, the assertion above would pass on anything."""
        body = self.body()

        self.assertGreaterEqual(body.count("text.slice("), 3)

    def test_overlapping_matches_are_merged(self):
        """Two phrases can overlap in one title, and a nested <mark> renders
        as a darker patch that reads like a third kind of match."""
        self.assertIn("merged", self.body())

    def test_an_unfiltered_view_is_left_alone(self):
        """No segments, no marks - and the same escaping as before."""
        body = self.body()

        self.assertIn("if (!segments.length) { return escapeHtml(text); }", body)

    def test_the_mark_takes_its_colour_from_the_palette(self):
        """A browser's default <mark> yellow belongs to neither theme."""
        with io.open(os.path.join(REPO_ROOT, "web", "style.css"),
                     encoding="utf-8") as handle:
            block = handle.read().split("mark.filter-hit {", 1)[1] \
                                 .split("}", 1)[0]

        self.assertIn("var(--", block)
        self.assertNotIn("#", block)


if __name__ == "__main__":
    unittest.main()
