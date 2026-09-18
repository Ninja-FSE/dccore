"""A language change has to repaint what is already on screen, not only
what the next poll happens to redraw.

applyTranslations() (web/app.js) walks every data-i18n-tagged element and
redoes the active view's header - both are enough for content that is
either static markup or re-set every language change already. It is NOT
enough for a view whose BODY is built once from data already in hand and
never repainted on a timer: Settings loads its categories and fields a
single time (state.settingsLoaded) and, unlike Downloads, Stats and the
notice/message lists - all of which repaint from their own poll loop
within a few seconds regardless of language - never refreshes itself in
the background. An operator sitting on the Settings page during a
language change would see the sidebar and the page header switch and the
panel underneath stay in the old language until they clicked a category
(which happens to call the same two render functions again) or reloaded
the page.

The fix has to re-read from state rather than re-fetch: a request while a
field is mid-edit could race the operator's own typing, and Settings is
the one view that deliberately never reloads on a timer for exactly that
reason (see loadSettings()'s preserveDirty parameter). renderSettingsRail()
and renderSettingsCategory() already read from state.settingsCategories
and already restore a dirty field's typed value from state.settingsDirty
rather than from field.value - the same repaint an ordinary category
switch already performs safely - so calling them again from a language
change carries no new risk.
"""

import io
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_js():
    with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
        return handle.read()


def function_body(source, name, *following):
    """The text of `function name(...) { ... }` up to the next function in
    `following` that appears after it - the same slicing idiom the other
    dashboard source-inspection tests use (see
    tests/test_every_setting_explains_itself.py's ThePageDrawsIt)."""
    start = source.index("function " + name + "(")
    block = source[start:]
    for other in following:
        marker = "function " + other + "("
        if marker in block:
            block = block[:block.index(marker)]
    return block


class ALanguageChangeRepaintsSettings(unittest.TestCase):

    def apply_translations(self):
        return function_body(app_js(), "applyTranslations", "loadLanguage")

    def test_it_repaints_the_settings_rail_and_category(self):
        block = self.apply_translations()
        self.assertIn("renderSettingsRail();", block,
                      "applyTranslations() does not redraw the settings "
                      "sidebar, so its labels stay in the old language "
                      "until the operator clicks a category or reloads")
        self.assertIn("renderSettingsCategory();", block,
                      "applyTranslations() does not redraw the open "
                      "settings category, so its labels and help text stay "
                      "in the old language until the operator clicks a "
                      "category or reloads")

    def test_the_repaint_only_fires_while_settings_is_the_active_view(self):
        """Calling these on every language change regardless of which view
        is open would rebuild a panel nobody is looking at for nothing, and
        - had it run before Settings ever loaded once - would crash on a
        category list that does not exist yet."""
        block = self.apply_translations()
        self.assertIn('state.active === "settings"', block)

    def test_the_repaint_requires_settings_to_already_be_loaded(self):
        """Without this, switching language before ever opening Settings
        would call renderSettingsCategory() against an empty
        state.settingsCategories, showing "no settings to show" until the
        operator visits the real page - the same failure the plain
        !state.settingsLoaded guard in activateView() exists to avoid."""
        block = self.apply_translations()
        self.assertIn("state.settingsLoaded", block)

    def test_the_repaint_reads_state_rather_than_asking_the_server_again(self):
        """A fresh fetch here could land while the operator is mid-edit and
        overwrite what they just typed - the reason Settings has no polling
        of its own in the first place. The fix must stay a pure repaint."""
        block = self.apply_translations()
        self.assertNotIn("loadSettings(", block,
                         "a language change must not re-fetch settings from "
                         "the server - see loadSettings()'s preserveDirty "
                         "parameter for why a background reload here is "
                         "unsafe while a field may be mid-edit")


if __name__ == "__main__":
    unittest.main()
