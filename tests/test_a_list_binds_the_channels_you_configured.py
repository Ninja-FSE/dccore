"""A served list is bound to a channel by picking it, not by retyping it.

Issue #368: Identity & network already holds the operator's channel list, and
the Served lists editor asked for each list's channels again as free text, so
the same names were typed a second time with nothing connecting the two.

IT IS NOT TYPING THAT MAKES THIS WORTH FIXING. library.list_for_request()
matches exactly, and its third rule is that once the primary list binds any
channels at all, a channel with nothing bound to it gets NOTHING - no advert,
no requests answered. So a single mistyped character does not bind one channel
wrongly. It silences the bot in the real channel, with no error raised
anywhere and nothing on the page saying why.

The first class below reproduces exactly that against the real routing, since
the whole justification rests on it.

WHAT THE PICKER DOES WITH A BINDING IT CANNOT OFFER. It keeps it, ticked, and
marks it. That is the operator's own data, and an install already in this
state is one where that row is the only place the mistake is visible - the
channel is silent, the log says nothing, and the file looks fine. Dropping it
silently would remove the diagnosis along with the symptom.

Nothing ticked still means every channel, which is what an empty field has
always meant and what every install today has.
"""

import io
import json
import os
import re
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import library  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

CHANNEL = "#somechannel"
TYPO = "#somechannnel"


def read(name):
    with io.open(os.path.join(REPO_ROOT, "web", name), encoding="utf-8") as f:
        return f.read()


def code_only():
    return re.sub(r"//[^\n]*", "", read("app.js"))


def function(name):
    text = code_only()
    head = text.index("function %s(" % name)
    line_start = text.rfind("\n", 0, head) + 1
    indent = " " * (head - line_start)
    return text[head:].split("\n" + indent + "}", 1)[0]


class WhatOneMistypedCharacterCosts(DCCoreTestCase):
    """The justification, reproduced. If this ever stops holding, the picker
    is a convenience and should be argued for as one."""

    def bind(self, *channels):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "lists.json")
        with io.open(path, "w", encoding="utf-8") as handle:
            json.dump([{"name": "Music", "primary": True,
                        "channels": list(channels),
                        "folders": [{"name": "music", "path": directory}]}],
                      handle)
        self.set_config(LISTS_FILE=path)

    def test_bound_correctly_the_channel_is_served(self):
        self.bind(CHANNEL)

        self.assertIsNotNone(library.list_for_request(CHANNEL))

    def test_bound_to_nothing_the_channel_is_served(self):
        """Every install today: one list, no bindings, answering everywhere."""
        self.bind()

        self.assertIsNotNone(library.list_for_request(CHANNEL))

    def test_one_wrong_character_and_the_channel_is_served_nothing(self):
        """Not a wrong list - NO list. No advert, no requests answered, and
        nothing anywhere reports it."""
        self.bind(TYPO)

        self.assertIsNone(library.list_for_request(CHANNEL))

    def test_and_the_typo_is_one_character(self):
        """Stated so the fixture cannot drift into an obviously-different
        string and make the trap look less easy to fall into."""
        self.assertEqual(len(TYPO), len(CHANNEL) + 1)
        self.assertIn(CHANNEL[1:6], TYPO)

    def test_case_alone_is_not_the_trap(self):
        """IRC channel names are case-insensitive and library.py already
        normalises, so this is about a genuinely different string."""
        self.bind(CHANNEL.upper())

        self.assertIsNotNone(library.list_for_request(CHANNEL))


class WhereTheOfferedChannelsComeFrom(unittest.TestCase):

    def source(self):
        return function("configuredChannels")

    def test_from_the_channel_setting_and_not_a_second_field(self):
        self.assertIn('settingsFieldByName("CHANNEL")', self.source())

    def test_a_pending_edit_wins_over_the_saved_value(self):
        """A channel typed into Identity & network and not yet saved is still
        one the operator means to be in. Offering the stale list would offer
        to bind a list to a channel they have just renamed."""
        body = self.source()

        self.assertIn("state.settingsDirty", body)
        self.assertIn("settingsValueToString(field.value)", body)

    def test_the_setting_is_a_comma_separated_list(self):
        self.assertIn('.split(",")', self.source())

    def test_duplicates_are_dropped(self):
        """Two boxes for one channel would let it be both ticked and not."""
        body = self.source()

        self.assertIn("seen[key]", body)
        self.assertIn("toLowerCase()", body)

    def test_a_missing_field_is_not_an_error(self):
        """The Served lists editor and the CHANNEL field are in different
        categories, so the field may not have been loaded."""
        self.assertIn("if (!field) { return []; }", self.source())


class TheControlOffersWhatIsConfigured(unittest.TestCase):

    def markup(self):
        return function("servedListChannelsHtml")

    def test_it_is_checkboxes_and_not_a_text_box(self):
        body = self.markup()

        self.assertIn("channelBoxHtml(", body)
        self.assertNotIn('type="text"', body)

    def test_the_old_free_text_input_is_gone(self):
        """Guard on the guard: leaving it would give two places to set the
        same thing, which is the whole complaint."""
        self.assertNotIn('class="served-list-channels" placeholder', code_only())

    def test_a_channel_name_never_reaches_the_markup(self):
        """It is operator input, and escapeHtml() does not encode a double
        quote - the rule every other row in this file follows."""
        box = function("channelBoxHtml")

        self.assertNotIn("escapeHtml(name)", box)
        self.assertNotIn("+ name +", box)

    def test_the_name_is_assigned_as_a_property_instead(self):
        attach = function("attachListRows")

        self.assertIn("box.dataset.channel = name;", attach)
        self.assertIn('label.querySelector("span").textContent = name;', attach)

    def test_with_no_channels_configured_it_says_where_to_add_them(self):
        """Rather than an empty box that looks broken."""
        body = self.markup()

        self.assertIn("No channels are configured yet", body)
        self.assertIn("Identity &amp; network", body)

    def test_and_says_what_happens_meanwhile(self):
        """Nothing ticked means everywhere, and an operator who cannot tick
        anything should be told that is not the same as being switched off."""
        self.assertIn("serves every channel", self.markup())


class ABindingThePickerCannotOfferIsKept(unittest.TestCase):
    """The part that matters most for anyone already caught by this."""

    def markup(self):
        return function("servedListChannelsHtml")

    def test_it_is_still_shown(self):
        body = self.markup()

        self.assertIn("var extra = bound.filter(", body)
        self.assertIn("!offeredKeys[name.toLowerCase()]", body)

    def test_ticked_because_it_is_bound(self):
        body = self.markup()

        self.assertIn("channelBoxHtml(index, name, true, true)", body)

    def test_and_marked_so_it_can_be_seen(self):
        body = self.markup()

        self.assertIn("is-unconfigured", function("channelBoxHtml"))
        self.assertIn("not in your join list", body)

    def test_the_warning_appears_only_when_there_is_one_to_give(self):
        """The GUARD, not the text under it. Asserting the words alone was
        satisfied with the condition replaced by false - the message sat in a
        branch nothing could reach, and a wrong binding went unmarked."""
        body = self.markup()

        self.assertIn("var warning = extra.length", body)
        self.assertIn('      : ""', body)

    def test_the_warning_says_what_to_do_about_it(self):
        body = self.markup()

        self.assertIn("Untick it", body)
        self.assertIn("Identity &amp; network", body)

    def test_the_mark_is_visible_in_the_stylesheet(self):
        css = read("style.css")
        rule = css.split(".served-list-channel.is-unconfigured {", 1)[1] \
                  .split("}", 1)[0]

        self.assertIn("var(--danger)", rule)

    def test_it_is_not_quietly_dropped(self):
        """A picker that silently discarded a value it could not represent
        would remove the diagnosis along with the symptom - the same rule the
        colour picker follows for a code its menus cannot say."""
        body = self.markup()

        self.assertIn(".concat(extra.map(", body)


class WhatGetsSaved(unittest.TestCase):

    def test_the_ticked_boxes_are_read_off_the_page(self):
        """Rather than accumulated as they are clicked: the draft is what gets
        sent, and one read of the boxes cannot disagree with them."""
        body = function("tickedChannels")

        self.assertIn("box.checked", body)
        self.assertIn("box.dataset.channel", body)

    def test_an_untitled_box_contributes_nothing(self):
        """Guard against a box whose name never got assigned putting an empty
        string into the binding, which would bind the list to nothing at all
        while looking bound."""
        self.assertIn("box.checked && box.dataset.channel", function("tickedChannels"))

    def test_the_draft_is_what_changes(self):
        attach = function("attachListRows")

        self.assertIn("draft[index].channels = tickedChannels(block)", attach)

    def test_the_order_drawn_and_the_order_read_are_derived_from_one_rule(self):
        """Two orders would pair a box with another channel's name."""
        names = function("channelNamesFor")

        self.assertIn("configuredChannels()", names)
        self.assertIn("!offeredKeys[name.toLowerCase()]", names)
        self.assertIn("offered.concat(", names)


class TheServerStillDecidesWhatIsValid(DCCoreTestCase):
    """The picker narrows what can be chosen; it does not become the rule.
    lists.json can be hand-edited, and #368 is explicit that the server side
    does not change."""

    def test_a_channel_list_is_still_accepted_as_written(self):
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "lists.json")
        with io.open(path, "w", encoding="utf-8") as handle:
            json.dump([{"name": "Music", "primary": True,
                        "channels": [CHANNEL],
                        "folders": [{"name": "music", "path": directory}]}],
                      handle)
        self.set_config(LISTS_FILE=path)

        entries = library.load_lists(path)

        self.assertEqual(entries[0].channels, [CHANNEL])

    def test_and_is_still_stored_folded(self):
        """The picker offers the operator's own spelling; the file keeps the
        normalised one, which is what list_for_channel() compares against."""
        directory = tempfile.mkdtemp()
        path = os.path.join(directory, "lists.json")
        with io.open(path, "w", encoding="utf-8") as handle:
            json.dump([{"name": "Music", "primary": True,
                        "channels": [CHANNEL.upper()],
                        "folders": [{"name": "music", "path": directory}]}],
                      handle)

        entries = library.load_lists(path)

        self.assertEqual(entries[0].channels, [CHANNEL])


if __name__ == "__main__":
    unittest.main()
