"""Four dashboard faults, three of them invisible rather than broken.

A CSS class that matches no rule does not fail, it does nothing - so the page
renders, the operator sees no error, and the styling simply never arrives.
That is why all four sat unnoticed:

  * #460 `foldersSectionHtml()` emitted `served-served-folder-rows`, a doubled
    prefix, so the rule for `served-folder-rows` never applied;
  * #461 `#import-status` had NO rule at all, so marking a rejected vars.ini
    with `is-error` coloured nothing;
  * #462 the password confirmation was written and then painted over by the
    reload on the very next line;
  * #459 the console pane appended forever, three elements per line, in the
    one view an operator leaves open.

The `is-error` test below is the general one: any element the JS marks as an
error must carry a class that can actually show it.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


def read(*parts):
    with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


class EveryErrorStateCanActuallyShow(unittest.TestCase):
    """#461, as a property over every site rather than the one that was wrong.

    `classList.toggle("is-error")` on an element whose classes have no
    `.<class>.is-error` rule is a no-op: the operator is told nothing, and
    nothing anywhere reports that the marking failed.
    """

    def test_every_element_marked_is_error_has_a_rule_that_can_match(self):
        js, html, css = read("web", "app.js"), read("web", "index.html"), read("web", "style.css")
        names = re.findall(r'el\.(\w+)\.classList\.toggle\("is-error"', js)
        id_by_name = dict(re.findall(
            r'(\w+):\s*document\.getElementById\("([a-z0-9-]+)"\)', js))

        self.assertTrue(names, "nothing toggles is-error any more - this guard "
                               "is now asserting over an empty set")

        unstyled = []
        for name in sorted(set(names)):
            element_id = id_by_name.get(name)
            if not element_id:
                continue
            # The WHOLE tag: class= precedes id= in this markup, so matching
            # from id= onward finds no classes and passes vacuously.
            tag = re.search(r'<[a-z]+[^>]*\bid="%s"[^>]*>' % re.escape(element_id), html)
            if not tag:
                continue
            classes = re.search(r'class="([^"]+)"', tag.group(0))
            classes = classes.group(1).split() if classes else []
            if not any(re.search(r'\.%s\.is-error\b' % re.escape(c), css) for c in classes):
                unstyled.append("%s (classes: %s)" % (element_id, classes or "none"))

        self.assertEqual(unstyled, [],
                         "these are marked as errors by app.js but no CSS rule "
                         "can colour them, so the marking does nothing")


class TheServedFoldersContainerIsStyled(unittest.TestCase):
    """#460. The emitted class carried a doubled prefix."""

    def test_the_class_the_js_emits_has_a_rule(self):
        js, css = read("web", "app.js"), read("web", "style.css")
        emitted = re.findall(r'class="(served[a-z-]*folder-rows)"', js)

        self.assertTrue(emitted, "the served-folders container is no longer "
                                 "emitted with a class - this guard has lost "
                                 "its subject")
        for name in set(emitted):
            self.assertRegex(css, r'\.%s\s*\{' % re.escape(name),
                             "app.js emits .%s and style.css has no rule for "
                             "it, so the container is unstyled" % name)

    def test_the_doubled_prefix_is_gone(self):
        self.assertNotIn("served-served", read("web", "app.js"))


class TheConfirmationSurvivesTheReload(unittest.TestCase):
    """#462. Written, then painted over by the reload on the next line."""

    def test_the_note_is_carried_rather_than_written_before_the_reload(self):
        """The wording is a translation key, not literal text - see
        web/lang/en.json's settings.passwordChangedRehashing."""
        js = read("web", "app.js")
        at = js.index("settings.passwordChangedRehashing")
        window = js[at - 400:at + 400]

        self.assertIn("state.settingsFlash", window,
                      "the confirmation is set directly again, so the reload "
                      "below repaints over it")
        self.assertNotIn("note.textContent = t(\"settings.passwordChangedRehashing", window,
                         "written straight onto the note again, which the "
                         "reload on the following line paints over")

    def test_the_carried_note_is_applied_after_the_panel_is_rebuilt(self):
        """Ordering is the property: applied before the repaint, it is
        destroyed by exactly the repaint it was meant to survive."""
        js = read("web", "app.js")
        render = js.split("function renderSettingsCategory", 1)[1]
        render = render.split("\n  function ", 1)[0]

        self.assertIn("applySettingsFlash()", render)
        self.assertLess(render.index("el.settingsFields.innerHTML = html"),
                        render.index("applySettingsFlash()"))


class TheConsolePaneIsBounded(unittest.TestCase):
    """#459. The server caps its buffer; the browser did not."""

    def test_appending_trims_the_oldest(self):
        js = read("web", "app.js")
        block = js.split("function appendConsoleLines", 1)[1]
        block = block.split(chr(10) + "  function ", 1)[0]

        self.assertIn("removeChild", block,
                      "nothing removes console lines, so the pane grows for as "
                      "long as the page is left open")
        self.assertIn("firstElementChild", block,
                      "trimmed from the wrong end - the newest lines are the "
                      "ones being read")

    def test_the_cap_is_above_the_servers_own_buffer(self):
        """Below it and the pane would drop lines the server still has, which
        would look like the log losing history rather than being bounded."""
        js = read("web", "app.js")
        cap = int(re.search(r"CONSOLE_LOG_MAX_ELEMENTS = (\d+)", js).group(1))

        self.assertGreater(cap, 500 * 3 - 1,
                           "three elements per line, against the server's "
                           "500-line buffer")
