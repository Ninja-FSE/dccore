"""A dropdown on the Settings page shows what is actually stored.

From the beta: "i set packet size to 64kb and when i press save and rehash i
see it back to 4kb".

The save had worked. 4096 is simply the FIRST DCC_BLOCK_SIZE choice, and no
<option> was ever marked `selected` - nor did anything assign select.value
after the markup was inserted - so every dropdown on the page rendered showing
its first choice whatever the daemon was using.

All four were affected, and the packet size was the least of them:

    LIST_FORMAT      always displayed 'txt'
    THEME            always displayed 'classic'
    ADMIN_CHAT_MODE  always displayed 'auto'
    DCC_BLOCK_SIZE   always displayed '4096'  -> "4 KB"

The worse half is not the one that was noticed. A page that states a value the
daemon is not using invites an operator to read it as correct and leave it
alone - agreeing to something they were never shown. It also cuts the other
way: someone who wants the first choice sees the first choice, changes
nothing, and keeps whatever was really there.

WHY IT SURVIVED: field.value arrives as JSON, so it is an int for
DCC_BLOCK_SIZE and a string for LIST_FORMAT, while an <option> value is always
text. Any strict comparison between the two is false for every numeric choice.
The fix compares as strings for that reason, and this file has a case for each
kind.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import settings_file  # noqa: E402
import webserver  # noqa: E402


def app_js():
    with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                 encoding="utf-8") as handle:
        return handle.read()


def choice_branch():
    """The `if (field.choices)` arm of settingsFieldHtml()."""
    body = app_js().split("function settingsFieldHtml(", 1)[1]
    return body.split("if (field.choices) {", 1)[1].split("} else if", 1)[0]


class EverySelectMarksItsCurrentOption(unittest.TestCase):

    def test_an_option_can_be_selected_at_all(self):
        """The defect in one line: no option ever carried the attribute."""
        self.assertIn("selected", choice_branch())

    def test_the_comparison_is_by_string(self):
        """field.value is an int for DCC_BLOCK_SIZE and a string for
        LIST_FORMAT; an <option> value is always text. A strict comparison
        between the two is false for every numeric choice, which is how this
        survived."""
        branch = choice_branch()

        self.assertIn("String(choice) === current", branch)

    def test_a_pending_edit_wins_over_the_stored_value(self):
        """A re-render while the save bar is dirty must not silently discard
        what the operator picked - the same rule the checkbox branch
        follows."""
        branch = choice_branch()

        self.assertIn("isDirty", branch)
        self.assertIn("state.settingsDirty[field.name]", branch)

    def test_nothing_assigns_select_value_after_the_fact(self):
        """Guard on the guard. If some later code set select.value after
        insertion, the markup would not have to be right and the assertions
        above would be testing nothing. Nothing does - which is why the bug
        existed."""
        source = app_js()
        assignments = re.findall(r"\.value\s*=\s*", source)
        settings_selects = [line for line in source.splitlines()
                            if ".value =" in line and "setting" in line.lower()]

        self.assertTrue(assignments, "the search itself is broken")
        self.assertEqual(settings_selects, [])


class TheOptionValuesStillMatchWhatTheSaveAccepts(unittest.TestCase):
    """Marking one selected is only correct if the values are the ones the
    server would take back."""

    def test_every_choice_field_offers_exactly_the_validated_set(self):
        payload = webserver.build_settings_payload()
        fields = {field["name"]: field
                  for category in payload["categories"]
                  for field in category["fields"]}

        for name, allowed in settings_file.CHOICES.items():
            with self.subTest(setting=name):
                self.assertIn(name, fields, f"{name} is not on the page")
                self.assertEqual(list(fields[name]["choices"]), list(allowed))

    def test_the_stored_value_is_one_of_the_offered_ones(self):
        """If it were not, the fix would render a select with NOTHING
        selected - which browsers draw as the first option, reintroducing the
        exact symptom."""
        payload = webserver.build_settings_payload()
        fields = {field["name"]: field
                  for category in payload["categories"]
                  for field in category["fields"]}

        for name in settings_file.CHOICES:
            with self.subTest(setting=name):
                field = fields[name]
                self.assertIn(str(field["value"]),
                              [str(c) for c in field["choices"]],
                              f"{name} holds a value the menu does not offer")

    def test_the_packet_size_menu_still_reads_in_kb(self):
        """The labels are what an operator picks by; the values stay bytes."""
        payload = webserver.build_settings_payload()
        field = [f for category in payload["categories"]
                 for f in category["fields"] if f["name"] == "DCC_BLOCK_SIZE"][0]

        self.assertEqual(field["choice_labels"][0], "4 KB")
        self.assertIn("64 KB", field["choice_labels"])
        self.assertEqual(field["choices"][0], "4096")


if __name__ == "__main__":
    unittest.main()
