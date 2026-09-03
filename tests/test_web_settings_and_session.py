"""Two frontend findings from the #162 audit, tracked on #177.

    #28  settingsFieldHtml() concatenated escapeHtml() output into value="…",
         and escapeHtml does not encode a double quote.
    #29  fetchJson() collapsed every non-2xx into Error("HTTP " + status), so
         an expired session rendered a healthy daemon as unreachable.

Both are in web/app.js, which has no test runner here - so these read the
source the way tests/test_web_theme.py and tests/test_web_layout.py already
do, plus a Python reimplementation of escapeHtml to demonstrate the actual
escaping gap rather than assert that a line changed.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(REPO_ROOT, "web")


def app_js():
    with io.open(os.path.join(WEB, "app.js"), encoding="utf-8") as handle:
        return handle.read()


def escape_html(value):
    """What web/app.js's escapeHtml() does: textContent in, innerHTML out.

    The browser encodes &, < and > that way. It does NOT encode a double
    quote, because inside a text node a quote needs no encoding - which is
    exactly why the output is safe as TEXT and unsafe in an ATTRIBUTE.
    """
    return (str(value).replace("&", "&amp;")
                      .replace("<", "&lt;")
                      .replace(">", "&gt;"))


class EscapeHtmlIsNotAnAttributeEscaper(unittest.TestCase):
    """The premise. If escapeHtml did encode quotes there would be nothing to
    fix, so this is worth stating before relying on it."""

    def test_it_encodes_the_tag_characters(self):
        self.assertEqual(escape_html("<script>"), "&lt;script&gt;")

    def test_it_leaves_a_double_quote_alone(self):
        self.assertEqual(escape_html('a"b'), 'a"b')

    def test_which_breaks_out_of_an_attribute(self):
        """Concatenated into value="…", the quote closes the attribute and
        everything after it is parsed as markup."""
        hostile = 'x" onfocus=alert(1) autofocus x'
        rendered = '<input value="' + escape_html(hostile) + '">'

        self.assertIn('value="x"', rendered)
        self.assertIn("onfocus=", rendered.split('value="x"')[1],
                      "the payload should land outside the attribute")

    def test_the_same_value_is_harmless_as_a_property(self):
        """The fix, stated as the property it restores: assigning to .value
        never parses markup at all, so no escaping question arises."""
        hostile = 'x" onfocus=alert(1) autofocus x'

        # There is no HTML in the assignment path - the string is the value.
        self.assertEqual(hostile, hostile)


class TheSettingsFormAssignsValuesAsProperties(unittest.TestCase):

    def field_builder(self):
        """The body of settingsFieldHtml()."""
        body = app_js()
        start = body.index("function settingsFieldHtml(")
        return body[start:body.index("\n  function ", start + 10)]

    def render_pass(self):
        """The body of renderSettingsCategory(), where the DOM already exists."""
        body = app_js()
        start = body.index("function renderSettingsCategory(")
        return body[start:body.index("\n  function ", start + 10)]

    def test_no_control_is_built_with_a_value_attribute(self):
        """The defect itself. Four other render functions in this file carry
        long comments about never doing this; this one did it twice."""
        offenders = [line.strip() for line in self.field_builder().splitlines()
                     if 'value="' in line and "option value=" not in line]

        self.assertEqual(offenders, [],
                         "a settings control still concatenates into an "
                         "attribute: " + "; ".join(offenders))

    def test_the_value_is_assigned_after_the_markup_is_in_the_dom(self):
        self.assertIn("input.value =", self.render_pass(),
                      "nothing assigns the value, so every field renders blank")

    def test_the_dirty_value_still_wins_over_the_saved_one(self):
        """A field mid-edit must survive a re-render - that is what the dirty
        map is for, and moving where the value is applied is exactly the kind
        of change that could quietly drop it.

        Asserts the assignment READS the dirty map, not merely that the name
        appears somewhere in the function: the hasOwnProperty check on the
        line above mentions it too, so the looser version of this passed with
        the dirty branch deleted.
        """
        window = self.render_pass()
        start = window.index("input.value")
        assignment = window[start:start + 200]

        self.assertIn("state.settingsDirty[field.name]", assignment,
                      "the assignment ignores an in-progress edit")
        self.assertIn("settingsValueToString(field.value)", assignment,
                      "the assignment ignores the saved value")

    def test_checkboxes_are_left_alone(self):
        """A checkbox carries its state in .checked, not .value; assigning
        .value to one would silently do nothing useful."""
        self.assertIn('input.type !== "checkbox"', self.render_pass())

    def test_the_select_still_offers_its_choices(self):
        """The options are still built as markup - they are the bot's own
        fixed vocabulary, not operator input - so removing the selected
        attribute must not have removed the options with it."""
        window = self.field_builder()

        self.assertIn("field.choices.map", window)
        self.assertIn("<option value=", window)


class AnExpiredSessionGoesToTheLoginPage(unittest.TestCase):
    """app.secret_key is os.urandom(32) per process, so every daemon restart
    invalidates every cookie. fetchJson turned the resulting 401 into
    Error("HTTP 401"), which the views render as the daemon being unreachable -
    so a perfectly healthy bot read as down until somebody reloaded by hand."""

    def fetch_json(self):
        body = app_js()
        start = body.index("function fetchJson(")
        return body[start:body.index("\n  function ", start + 10)]

    def test_a_401_is_recognised_at_all(self):
        self.assertIn("res.status === 401", self.fetch_json())

    def test_it_navigates_to_the_login_page(self):
        self.assertIn("/login", self.fetch_json())

    def test_the_check_comes_before_the_generic_error(self):
        """Order matters: `if (!res.ok) throw` first would swallow the 401
        before anything could look at it."""
        window = self.fetch_json()

        self.assertLess(window.index("401"), window.index("!res.ok"),
                        "the generic error is thrown before the 401 is seen")

    def test_other_failures_still_raise(self):
        """The control. Redirecting on everything would hide a real 500."""
        self.assertIn('throw new Error("HTTP " + res.status)', self.fetch_json())

    def test_the_promise_is_left_pending_during_navigation(self):
        """Resolving or rejecting would flash an error into a page that is
        already being replaced."""
        self.assertIn("new Promise(function () {})", self.fetch_json())


class TheGuardTestThatCouldNotSeeThis(unittest.TestCase):
    """tests/test_webserver.py's XSS guard greps for data-bot=/data-filename=/
    data-folder= specifically, which is why it never covered the settings form.
    Widened here rather than there, to keep this finding's evidence in one
    place."""

    # Matches `value="' +` - an attribute value opened and then continued by
    # string concatenation. The option list is exempt: its contents are the
    # bot's own fixed vocabulary, not operator input.
    ATTRIBUTE_CONCAT = re.compile('value=["\']["\']?\\s*\\+')

    def test_every_data_setting_control_is_attribute_safe(self):
        """The general property: no line in app.js may concatenate anything
        into an attribute value except the option list."""
        offenders = []
        for number, line in enumerate(app_js().splitlines(), 1):
            if "option value=" in line:
                continue
            if self.ATTRIBUTE_CONCAT.search(line):
                offenders.append(f"app.js:{number}: {line.strip()[:70]}")

        self.assertEqual(offenders, [],
                         "concatenation into an attribute value: "
                         + "; ".join(offenders))

    def test_the_scan_would_have_caught_the_original(self):
        """Fixture invariant. A regex matching nothing would make the test
        above pass on the very code it exists to reject - which is how the
        guard in tests/test_webserver.py missed this form in the first
        place."""
        original = 'control = \'<input type="text" data-setting="\' + escapeHtml(field.name) + \'" value="\' + escapeHtml(v) + \'">\';'
        fixed = 'control = \'<input type="text" data-setting="\' + escapeHtml(field.name) + \'">\';'

        self.assertIsNotNone(self.ATTRIBUTE_CONCAT.search(original))
        self.assertIsNone(self.ATTRIBUTE_CONCAT.search(fixed))


if __name__ == "__main__":
    unittest.main()
