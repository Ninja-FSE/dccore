"""A "quoted phrase" in a search means those words together, in order (#774).

Asked by the operator after `@find Metal Church` answered 6516 results:
every word of a search had to appear somewhere on a file's line, in any
order, so a band named with two common words matched every file holding
both - Metallica, Heavy Metal, Churchill. A quoted term was no help: the
quotes were searched for literally and matched nothing.

Now a part in double quotes is a phrase - its words adjacent, in order,
with only separators between them - and words outside the quotes keep the
old rule. A term with no quotes is split exactly as before; that is the
first thing checked, against a copy of the old rule.
"""

import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import list as list_mod  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase, RecordingSocket  # noqa: E402
from tests.test_webserver import write_master_list  # noqa: E402


def the_old_rule(term):
    """execute_search()'s and split_list_search_words()'s split before #774."""
    clean_term = re.sub(r'[-*_.]', ' ', str(term or ""))
    return [w.strip().lower() for w in clean_term.split() if w.strip()]


ROWS = [
    ("Metal Church - The Dark.mp3", "5.1MB"),                  # phrase, spaced
    ("Metal_Church-Watch The Children Pray.flac", "31MB"),     # underscore
    ("metal.church.live.1986.mp3", "7MB"),                     # dots, and 1986
    ("Metallica - Church Bells.mp3", "4MB"),                   # both words, not adjacent
    ("Church Of Heavy Metal.mp3", "3MB"),                      # both words, wrong order
    ("Vivaldi - The Four Seasons - Winter.flac", "40MB"),
]


class TheSplit(unittest.TestCase):

    def test_a_term_without_quotes_splits_exactly_as_it_always_did(self):
        for term in ("Metal Church", "vivaldi   winter", "AC-DC_Back.in*Black", "Björk Jóga",
                     "  ", "", "---", "a.b-c_d*e", "Metal Church 1986"):
            with self.subTest(term=term):
                self.assertEqual(list_mod.split_search_term(term), the_old_rule(term))

    def test_a_quoted_part_is_one_phrase(self):
        self.assertEqual(list_mod.split_search_term('"Metal Church"'), [("metal", "church")])

    def test_words_outside_the_quotes_keep_the_old_rule(self):
        self.assertEqual(list_mod.split_search_term('"Metal Church" 1986'),
                         ["1986", ("metal", "church")])

    def test_separators_inside_the_quotes_split_the_phrase_the_same_way(self):
        self.assertEqual(list_mod.split_search_term('"Metal_Church-Live"'),
                         [("metal", "church", "live")])

    def test_a_one_word_phrase_is_just_a_word(self):
        self.assertEqual(list_mod.split_search_term('"metal"'), ["metal"])

    def test_an_unpaired_quote_is_dropped_not_searched_for(self):
        """It used to be part of the word, so `@find "metal` matched nothing."""
        self.assertEqual(list_mod.split_search_term('"metal'), ["metal"])

    def test_the_dashboard_splits_with_the_same_function(self):
        for term in ('"Metal Church" 1986', "plain words", '"a b"'):
            with self.subTest(term=term):
                self.assertEqual(webserver.split_list_search_words(term),
                                 list_mod.split_search_term(term))


class TheMatch(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)
        self.path = write_master_list(self.tree.lists, "SomeBot", [(None, ROWS)])

    def matched(self, term):
        entries, total = list_mod.find_matching_entries(
            list_mod.split_search_term(term), list_path=self.path)
        self.assertEqual(total, len(entries))
        return sorted(entry["filename"] for entry in entries)

    def test_unquoted_it_matches_both_words_anywhere_as_before(self):
        self.assertEqual(self.matched("Metal Church"), sorted([
            "Church Of Heavy Metal.mp3", "Metal Church - The Dark.mp3",
            "Metal_Church-Watch The Children Pray.flac", "Metallica - Church Bells.mp3",
            "metal.church.live.1986.mp3"]))

    def test_quoted_it_matches_only_the_words_together_in_order(self):
        self.assertEqual(self.matched('"Metal Church"'), sorted([
            "Metal Church - The Dark.mp3", "Metal_Church-Watch The Children Pray.flac",
            "metal.church.live.1986.mp3"]))

    def test_a_word_outside_the_phrase_narrows_it_further(self):
        self.assertEqual(self.matched('"Metal Church" 1986'), ["metal.church.live.1986.mp3"])

    def test_two_phrases_must_both_be_there(self):
        self.assertEqual(self.matched('"four seasons" "the four"'),
                         ["Vivaldi - The Four Seasons - Winter.flac"])
        self.assertEqual(self.matched('"four seasons" "metal church"'), [])

    def test_an_empty_search_still_matches_everything(self):
        self.assertEqual(len(self.matched("")), len(ROWS))


class ThroughFind(DCCoreTestCase):
    """@find itself, against the same list, as a user would type it."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)
        write_master_list(self.tree.lists, "DCCoreTest", [(None, ROWS)])
        self.set_config(FILE_DIRECTORY=self.tree.music, LOCAL_LIST_DIR=self.tree.lists,
                        LIST_BASE_NAME="DCCoreTest", NICKNAME="DCCoreTest", CHANNEL="#chan",
                        search_inprogress=False, update_inprogress=False)

    def find(self, term):
        self.oserve.queued.clear()
        list_mod.execute_search(RecordingSocket(), "dave", term, "#chan")
        return "".join(m for _u, m, *_ in self.oserve.queued)

    def test_the_quoted_search_answers_only_the_band(self):
        reply = self.find('"Metal Church"')

        self.assertIn("Found: ", reply)
        self.assertIn("3 Match(es)", reply)
        self.assertNotIn("Metallica", reply)
        self.assertNotIn("Church Of Heavy Metal", reply)

    def test_the_unquoted_search_answers_as_it_always_did(self):
        self.assertIn("5 Match(es)", self.find("Metal Church"))


if __name__ == "__main__":
    unittest.main()
