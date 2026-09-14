"""A list with nothing matching leaves the sidebar, and says it has.

Asked for during the RC1 beta: "names get hidden as you type something that
you search lists for. only the names with result are shown. and you can still
select/deselect results from certain bots if you click on their names and then
reclick on their names (toggle/select)."

The second half of that already existed - clicking a name while filtering
toggles its results, and "Show all lists" / "Show none" do it wholesale - which
makes this partly a discoverability problem. What changed is the first half.

WHAT IT REPLACED, AND WHY THAT WAS NOT SIMPLY WRONG. The rows were dimmed, and
the argument was written into the stylesheet: the sidebar is also the answer to
"who has this", and a row that vanished would take that answer with it. That is
right about what matters. It is wrong about what to do - dimming asks the
operator to scan thirty rows and judge opacity, where a count states the same
fact outright, and a button beside it puts them back for anyone who wants to
look.

So the answer is kept and made easier to read: "12 with no match" is the same
information as twelve faded rows, in a form nobody has to count.

FOUR ROWS ARE NEVER HIDDEN, and none of them is a special case for its own
sake. Our own list, which this filter does not search. The list currently open,
or the table would show a list with no row for it. The row holding keyboard
focus, because hiding it drops focus to the body and loses the operator's
place. And all of them, once the operator has asked to see them.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def read(name):
    with io.open(os.path.join(REPO_ROOT, "web", name), encoding="utf-8") as f:
        return f.read()


def function(name):
    return read("app.js").split("function %s(" % name, 1)[1].split("\n  }", 1)[0]


class ARowWithNothingLeavesTheList(unittest.TestCase):

    def test_it_is_taken_off_the_screen_and_not_only_faded(self):
        self.assertIn('row.classList.toggle("is-filtered-away", away)',
                      function("applyFilterHighlight"))

    def test_the_rule_removes_it_from_the_layout_entirely(self):
        """visibility or opacity would leave a gap in the list and a stop on
        the way through it with the keyboard."""
        css = read("style.css")
        rule = css.split(".bot-row.is-filtered-away {", 1)[1].split("}", 1)[0]

        self.assertIn("display: none", rule)

    def test_only_while_something_is_being_searched_for(self):
        body = function("applyFilterHighlight")
        decision = re.search(r"var nothing = ([^;]+);", body)

        self.assertTrue(decision, "the emptiness test has moved")
        self.assertIn("filtering", decision.group(1))

    def test_a_bot_is_empty_because_the_server_said_so(self):
        """Not because the page came back without its rows. The page is
        capped, so a bot whose matches all fall past the cap would look empty
        when it is not."""
        body = function("applyFilterHighlight")

        self.assertIn("payload.empty", body)


class WhatIsNeverHidden(unittest.TestCase):

    def decision(self):
        body = function("applyFilterHighlight")
        found = re.search(r"var away = ([^;]+);", body)
        self.assertTrue(found, "the hide decision has moved")
        return found.group(1)

    def test_the_decision_was_found(self):
        """Guard on the guard - a decision that failed to lift would make
        every assertion below vacuous."""
        self.assertTrue(self.decision().strip())

    def test_our_own_list(self):
        """This filter searches the lists FETCHED from other bots. Ours is not
        one of them, so it can never be the one with nothing to show."""
        body = function("applyFilterHighlight")

        self.assertIn("!isOwnSource(bot)", body)

    def test_the_list_that_is_currently_open(self):
        """Or the table would be showing a list with no row for it."""
        body = function("applyFilterHighlight")

        self.assertIn("state.filelistsSource", body)
        self.assertIn("pinned", self.decision())

    def test_the_row_holding_keyboard_focus(self):
        """Hiding the focused element drops focus to the body, which loses the
        operator's place in a list they were moving through."""
        body = function("applyFilterHighlight")

        self.assertIn("row.contains(document.activeElement)", body)

    def test_and_all_of_them_once_the_operator_asks(self):
        self.assertIn("!state.filelistsRevealEmpty", self.decision())

    def test_a_row_that_cannot_be_hidden_is_still_marked(self):
        """It is on screen, and the fact that it has nothing is still the
        thing worth knowing about it."""
        self.assertIn('row.classList.toggle("is-filtered-out", nothing && !away)',
                      function("applyFilterHighlight"))


class TheCountIsTheAnswerTheRowsUsedToGive(unittest.TestCase):

    def test_the_hidden_ones_are_counted(self):
        body = function("applyFilterHighlight")

        self.assertIn("if (away) { hidden += 1; }", body)
        self.assertIn("renderRevealButton(filtering, hidden)", body)

    def test_the_button_says_how_many(self):
        self.assertIn('"Show " + hidden + " with no match"',
                      function("renderRevealButton"))

    def test_and_offers_the_way_back_out_again(self):
        """A toggle, not a one-way door: somebody who looked at what was
        hidden wants it back without retyping the term."""
        self.assertIn('"Hide lists with no match"', function("renderRevealButton"))

    def test_nothing_hidden_means_no_button(self):
        """It would otherwise sit there offering to reveal nothing, which
        reads as though something is being kept from you."""
        body = function("renderRevealButton")

        self.assertIn("if (!filtering || (!hidden && !state.filelistsRevealEmpty)) {",
                      body)
        self.assertIn("button.hidden = true;", body)

    def test_it_is_still_offered_while_they_are_showing(self):
        """The count is zero exactly when they are all revealed, so a bare
        `!hidden` would take away the button that hides them again."""
        self.assertIn("!state.filelistsRevealEmpty", function("renderRevealButton"))

    def test_the_button_exists_on_the_page(self):
        page = read("index.html")

        self.assertIn('id="filelists-filter-reveal"', page)
        self.assertIn("filelists-filter-reveal", read("app.js"))

    def test_it_sits_with_the_other_two_that_change_what_is_shown(self):
        """Show all lists / Show none / Show N with no match are one group,
        and a control that appeared somewhere else would read as unrelated."""
        page = read("index.html")
        actions = page.split('id="filelists-filter-actions"', 1)[1] \
                      .split("</div>", 1)[0]

        self.assertIn('id="filelists-filter-reveal"', actions)

    def test_it_starts_hidden(self):
        page = read("index.html")
        button = page.split('id="filelists-filter-reveal"', 1)[1].split(">", 1)[0]

        self.assertIn("hidden", button)


class ANewTermIsANewQuestion(unittest.TestCase):

    def test_the_revealed_rows_do_not_carry_over(self):
        """A row put back on screen while looking for one thing should not
        still be there, unasked, while looking for the next - the same rule
        the per-bot exclusions already follow."""
        body = function("runFilelistsFilter")

        self.assertIn("state.filelistsRevealEmpty = false;", body)
        self.assertIn("state.filelistsExcluded = {};", body)

    def test_it_starts_off(self):
        source = read("app.js")
        declared = source.split("filelistsRevealEmpty:", 1)[1].split(",", 1)[0]

        self.assertEqual(declared.strip(), "false")

    def test_the_toggle_redraws_rather_than_re_asking_the_server(self):
        """The rows are already in the browser. Re-running the search to show
        rows it already returned would be slower than the search itself."""
        source = read("app.js")
        handler = source.split("el.filelistsFilterReveal.addEventListener", 1)[1] \
                        .split("});", 1)[0]

        self.assertIn("state.filelistsRevealEmpty = !state.filelistsRevealEmpty",
                      handler)
        self.assertIn("rerenderFromFilterPayload()", handler)
        self.assertNotIn("loadFilelists()", handler)


class TheOldArgumentIsAnsweredRatherThanDeleted(unittest.TestCase):
    """The stylesheet said outright why these rows were dimmed and not hidden.
    Changing the behaviour without changing that note would leave the file
    explaining a decision it no longer describes - which is worse than no note,
    because it reads as current."""

    def comment(self):
        css = read("style.css")
        before = css.split(".bot-row.is-filtered-out {", 1)[0]
        return before.rsplit("/*", 1)[1]

    def test_the_note_no_longer_claims_the_rows_stay(self):
        self.assertNotIn("Dimmed rather than hidden", self.comment())

    def test_it_says_what_dimming_is_still_for(self):
        """The class did not go away: it is what a row gets when it cannot be
        hidden, or when the operator has asked to see it."""
        text = self.comment()

        self.assertIn("currently open", text)
        self.assertIn("focus", text)

    def test_and_keeps_the_point_the_old_note_was_making(self):
        """"Who has this" is still the question the sidebar answers. What
        changed is the form of the answer."""
        self.assertIn("who has this", self.comment())


if __name__ == "__main__":
    unittest.main()
