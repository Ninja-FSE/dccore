"""The colour on a bot's name is the one the legend promises.

FROM THE BETA, looking at a sidebar where every not-downloaded bot was grey:

    "shouldnt names be red of those not downloaded yet?"

They should. The name had just taken over carrying the list's freshness - green
current, orange changed, red not downloaded - and a rule written for the older
design was still there:

    .bot-row[data-held="no"] .bot-row-name { color: var(--text-dim); }

That is specificity (0,3,0) against the modifier's (0,2,0), on EXACTLY the rows
the modifier is about. So every not-downloaded bot stayed dim while the legend
beside it promised red, and nothing failed: the class was applied, the rule
existed, the colour was simply outranked.

WHY THIS FILE EXISTS RATHER THAN A ONE-LINE FIX. A specificity collision is
invisible in review - both rules are correct on their own, and the losing one
is present and spelled right - and silent at runtime. Nothing in this project
executes CSS, so the cascade is checked by computing it.

The check is deliberately narrow: only rules that set a COLOUR on
.bot-row-name, and only against the four modifiers. A general CSS engine is not
what is wanted here; knowing that these four are the last word on their own
element is.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

FRESHNESS = ("is-current", "is-changed", "is-not-held", "is-unknown")


def stylesheet():
    with io.open(os.path.join(REPO_ROOT, "web", "style.css"),
                 encoding="utf-8") as handle:
        return handle.read()


def specificity(selector):
    """(ids, classes, elements) for one selector, near enough for this file.

    Counts #id, .class, [attr] and :pseudo-class as CSS does, and bare element
    names as elements. ::pseudo-elements are not used on these rules and are
    not modelled.
    """
    ids = len(re.findall(r"#[\w-]+", selector))
    classes = (len(re.findall(r"\.[\w-]+", selector))
               + len(re.findall(r"\[[^\]]+\]", selector))
               + len(re.findall(r"(?<!:):[a-z-]+(?:\([^)]*\))?", selector)))
    elements = len(re.findall(r"(?:^|[\s>+~])([a-z][\w-]*)", selector))
    return (ids, classes, elements)


def colour_rules_on(target):
    """Every rule that sets a colour on `target`, as (selector, specificity).

    Comments are stripped first: this file explains its own history in them,
    and the selector it is explaining appears there in full.
    """
    css = re.sub(r"/\*.*?\*/", "", stylesheet(), flags=re.S)
    found = []
    for selector, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if not re.search(r"(?<!-)\bcolor\s*:", body):
            continue
        for part in selector.split(","):
            part = part.strip()
            if target in part:
                found.append((part, specificity(part)))
    return found


class TheSpecificityReaderItself(unittest.TestCase):
    """Guard on the guard. A calculator that returned the same number for
    everything would pass every assertion below."""

    def test_a_class_beats_an_element(self):
        self.assertGreater(specificity(".name"), specificity("span"))

    def test_two_classes_beat_one(self):
        self.assertGreater(specificity(".a.b"), specificity(".a"))

    def test_an_attribute_counts_as_a_class(self):
        self.assertEqual(specificity('[data-held="no"]'), specificity(".a"))

    def test_the_collision_that_started_this_is_measured_correctly(self):
        older = specificity('.bot-row[data-held="no"] .bot-row-name')
        modifier = specificity(".bot-row-name.is-not-held")

        self.assertEqual(older, (0, 3, 0))
        self.assertEqual(modifier, (0, 2, 0))
        self.assertGreater(older, modifier)

    def test_it_finds_the_rules_it_is_pointed_at(self):
        self.assertTrue(colour_rules_on(".bot-row-name"),
                        "the rule reader found nothing to check")


class NothingOutranksTheFreshnessColour(unittest.TestCase):

    def modifier_rules(self):
        return {selector: weight
                for selector, weight in colour_rules_on(".bot-row-name")
                if any(name in selector for name in FRESHNESS)}

    def test_all_four_states_have_a_rule(self):
        selectors = " ".join(self.modifier_rules())

        for name in FRESHNESS:
            self.assertIn(name, selectors,
                          name + " sets no colour on .bot-row-name")

    def test_no_other_rule_outranks_them(self):
        """The defect, stated generally: a rule that wins on the same element
        makes the modifier a class that is applied and does nothing."""
        modifiers = self.modifier_rules()
        others = [(selector, weight)
                  for selector, weight in colour_rules_on(".bot-row-name")
                  if selector not in modifiers]

        for selector, weight in others:
            for modifier, modifier_weight in modifiers.items():
                self.assertLessEqual(
                    weight, modifier_weight,
                    f"{selector!r} sets a colour on .bot-row-name and "
                    f"outranks {modifier!r}, so the freshness colour never "
                    f"shows on the rows it applies to")

    def test_the_old_override_is_gone(self):
        """Named directly as well as caught generally: this exact selector is
        the one that was reported, and a general rule is easy to satisfy by
        weakening it."""
        for selector, _weight in colour_rules_on(".bot-row-name"):
            self.assertNotIn('data-held="no"', selector,
                             "the dim-when-not-held colour is back")

    def test_the_legend_promises_the_same_four(self):
        """A colour nothing renders, or a row colour nothing names, is the
        other half of the same confusion."""
        with io.open(os.path.join(REPO_ROOT, "web", "index.html"),
                     encoding="utf-8") as handle:
            markup = handle.read()

        for name in FRESHNESS:
            self.assertIn("legend-name " + name, markup)


class ABotWeHoldNothingFromIsStillDistinguishable(unittest.TestCase):
    """Removing the colour override must not remove the distinction it was
    making - only stop it fighting the one that replaced it."""

    def test_it_is_still_styled_by_something(self):
        css = re.sub(r"/\*.*?\*/", "", stylesheet(), flags=re.S)

        self.assertIn('.bot-row[data-held="no"] .bot-row-name', css,
                      "the not-held row lost its own styling entirely")

    def test_but_not_by_a_colour(self):
        rule = re.search(
            r'\.bot-row\[data-held="no"\] \.bot-row-name\s*\{([^}]*)\}',
            re.sub(r"/\*.*?\*/", "", stylesheet(), flags=re.S))

        self.assertIsNotNone(rule)
        self.assertNotIn("color", rule.group(1))


if __name__ == "__main__":
    unittest.main()
