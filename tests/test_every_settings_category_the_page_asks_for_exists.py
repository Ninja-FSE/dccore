"""A category id the page keys off must be one the server actually sends.

WHAT THIS IS FOR. Three of the dashboard's editors are not settings and cannot
be. Served folders and "Serve more than one list" are ORDERED lists of
{label, path} validated as a set; On connect is a list of commands with an
order that matters. Each lives in its own JSON file behind its own endpoint -
see build_folders_payload() in webserver.py - and each is then stitched into a
settings CATEGORY, so an operator finds it where they would look for it.

That stitching is a string comparison against `category.id`, and nothing on
either side checks that the string names a category that exists.

WHAT WENT WRONG. Regrouping the Settings page renamed the categories. All 94
settings still appeared - every one of them is listed in a category, and a
sweep for settings that had fallen off the page would have found nothing. But
the three editors were attached to an id that no longer existed, so all three
stopped being drawn anywhere, and nothing failed: no error, no empty panel, no
console warning. A feature reachable only from the dashboard became reachable
from nowhere.

Reported from the beta, which is the only way it could have been found:
"what happened with multi folder / channel? cant see it in settings at all".

WHY THIS TEST AND NOT A CONSTANT. Naming the ids as variables in app.js is
worth doing and does not help: a renamed category still leaves the variable
holding a string nothing matches. The two sides have to be compared, and only
a test can do that - webserver.py owns the category list and app.js owns the
attachment, and neither imports the other.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import webserver  # noqa: E402


def source():
    with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as f:
        return f.read()


def code_only():
    """Comments in this file quote code at length, and a dead id named in one
    of them is a note about history rather than a live comparison."""
    return re.sub(r"//[^\n]*", "", source())


END_OF_BLOCK = "\n    }"


def category_ids():
    return [entry[0] for entry in webserver.SETTINGS_CATEGORIES]


class EveryIdThePageComparesAgainst(unittest.TestCase):

    def asked_for(self):
        """Both spellings: the literal, and the constant that holds one."""
        text = code_only()
        found = set(re.findall(r'category\.id === "([^"]+)"', text))
        found |= set(re.findall(r'settingsActiveCategory === "([^"]+)"', text))
        for name in re.findall(r'var ([A-Z_]*CATEGORY) = "([^"]+)"', text):
            found.add(name[1])
        return found

    def test_some_are_asked_for(self):
        """Guard on the guard. With none found the assertion below would pass
        on an empty set, which is exactly the state this test exists to catch
        being indistinguishable from a healthy one."""
        self.assertTrue(self.asked_for(),
                        "no category id is compared anywhere - has the "
                        "stitching moved, or has the pattern gone stale?")

    def test_each_one_is_a_category_the_server_sends(self):
        known = category_ids()

        for wanted in sorted(self.asked_for()):
            with self.subTest(category=wanted):
                self.assertIn(
                    wanted, known,
                    "app.js draws something on the %r category, which "
                    "SETTINGS_CATEGORIES does not define - so whatever it "
                    "draws appears nowhere, and nothing fails. Known: %s"
                    % (wanted, ", ".join(known)))


class TheThreeEditorsHaveSomewhereToBe(unittest.TestCase):
    """Named individually, because "every id resolves" is also true of a page
    that has stopped drawing them at all."""

    def branch(self, constant):
        """What is drawn under one category, up to the end of that block."""
        text = code_only()
        marker = "if (category.id === %s) {" % constant
        self.assertIn(marker, text, "nothing is drawn on %s at all" % constant)
        return text.split(marker, 1)[1].split(END_OF_BLOCK, 1)[0]

    def test_the_served_folders_editor_is_placed(self):
        """The CALL, inside the branch that draws it. The bare name is also
        the name of its own definition, so asserting that alone passes on a
        page that never calls it."""
        branch = self.branch("SERVED_FOLDERS_CATEGORY")

        self.assertIn("foldersSectionHtml()", branch)

    def test_the_multi_list_editor_shares_that_category(self):
        """One editor, not two: with more than one list configured the folders
        live inside the lists, so the single-list editor there as well would
        be a second place to edit folders that quietly does nothing."""
        branch = self.branch("SERVED_FOLDERS_CATEGORY")

        self.assertIn("listsSectionHtml()", branch)
        self.assertIn("foldersSectionHtml()", branch)

    def test_the_on_connect_editor_is_placed(self):
        self.assertIn("html += onConnectSectionHtml();",
                      self.branch("ON_CONNECT_CATEGORY"))

    def test_each_one_is_also_wired_up_after_it_is_drawn(self):
        """Drawing the rows without attaching them gives an editor whose
        fields do not record what is typed into them - which looks like it
        works right up until Save."""
        text = code_only()

        for drawn, attached in (("SERVED_FOLDERS_CATEGORY", "attachFolderRows()"),
                                ("SERVED_FOLDERS_CATEGORY", "attachListRows()"),
                                ("ON_CONNECT_CATEGORY", "attachOnConnectRows()")):
            with self.subTest(attach=attached):
                after = text.split("el.settingsFields.innerHTML = html;", 1)[1]
                self.assertIn(attached, after)
                self.assertIn(drawn, after)

    def test_the_folder_editor_sits_with_the_directory_it_overrides(self):
        """Music directory is labelled "used only when no folders are set", so
        the thing that sets them belongs in the same category - otherwise the
        label points at a screen the operator has to go and find."""
        text = code_only()
        chosen = re.search(r'var SERVED_FOLDERS_CATEGORY = "([^"]+)"', text).group(1)

        for cid, _label, names in webserver.SETTINGS_CATEGORIES:
            if cid == chosen:
                self.assertIn("FILE_DIRECTORY", names)
                return
        self.fail("SERVED_FOLDERS_CATEGORY names no category at all")

    def test_the_on_connect_editor_sits_with_the_server_it_talks_to(self):
        """These commands run after registration and before the JOIN, so they
        belong beside the server, the nick and the channels - not beside the
        library."""
        text = code_only()
        chosen = re.search(r'var ON_CONNECT_CATEGORY = "([^"]+)"', text).group(1)

        for cid, _label, names in webserver.SETTINGS_CATEGORIES:
            if cid == chosen:
                self.assertIn("SERVER", names)
                self.assertIn("CHANNEL", names)
                return
        self.fail("ON_CONNECT_CATEGORY names no category at all")


class NoSettingFallsOffThePageEither(unittest.TestCase):
    """The other half of the same question, and the half that was already
    fine - stated so a future regroup is checked on both."""

    def test_every_declared_setting_appears_in_a_category(self):
        import defaults as config
        import settings_file

        declared = set(settings_file.declared_types(vars(config)))
        shown = set()
        for _cid, _label, names in webserver.SETTINGS_CATEGORIES:
            shown |= set(names)

        # Neither is something an operator types into a box: the password has
        # its own route (see apply_settings_changes, which refuses it), and
        # the version is what the daemon reports, not what it is told.
        missing = declared - shown - {"ADMIN_PASSWORD_HASH", "SCRIPT_VERSION"}

        self.assertEqual(sorted(missing), [],
                         "declared but on no category, so unreachable from "
                         "the dashboard")

    def test_no_category_lists_a_setting_that_does_not_exist(self):
        import defaults as config
        import settings_file

        declared = set(settings_file.declared_types(vars(config)))
        for cid, _label, names in webserver.SETTINGS_CATEGORIES:
            for name in names:
                with self.subTest(category=cid, setting=name):
                    self.assertIn(name, declared,
                                  "%s is on the page and is not a setting" % name)


class EverythingElseThePageNamesByString(unittest.TestCase):
    """The same shape, swept for everywhere else it occurs.

    Asked for after the category bug was found: "Also check everything if it
    exists in webpage". It did not find a second instance - these guard against
    the next one, because the whole point of this failure mode is that it is
    invisible until somebody opens the screen and the thing is not there.

    NOT INCLUDED: a sweep for CSS classes the JS toggles that no rule defines.
    It was run and came back clean, but it cannot tell a missing style from a
    class used purely as a selector hook, and a guard that has to be argued
    with every time it fires is one that gets deleted.
    """

    def html(self):
        with io.open(os.path.join(REPO_ROOT, "web", "index.html"),
                     encoding="utf-8") as handle:
            return handle.read()

    def page_ids(self):
        return set(re.findall(r'id="([^"]+)"', self.html()))

    def test_every_id_the_script_looks_up_is_on_the_page(self):
        """A missing id makes el.X null. Nothing throws until something reads
        a property of it, which may be on a screen nobody opens for weeks."""
        wanted = set(re.findall(r'getElementById\("([^"]+)"\)', code_only()))

        self.assertTrue(wanted, "no lookups found - has the pattern gone stale?")
        self.assertEqual(sorted(wanted - self.page_ids()), [])

    def test_and_every_one_used_with_a_selector(self):
        wanted = set(re.findall(r'querySelector\("#([A-Za-z0-9_-]+)"\)',
                                code_only()))

        self.assertEqual(sorted(wanted - self.page_ids()), [])

    def test_every_el_name_used_is_one_that_was_declared(self):
        """`el` is built once from getElementById. A name never put in it is
        undefined, and reading a property of undefined stops the render that
        is running - taking the rest of the screen with it."""
        source_text = source()
        declared = set(re.findall(r"^\s{4}([A-Za-z0-9_]+):\s*document\.",
                                  source_text, re.M))
        used = set(re.findall(r"\bel\.([A-Za-z0-9_]+)\b", code_only()))

        self.assertTrue(declared, "the el map has moved")
        self.assertEqual(sorted(used - declared), [])

    def test_every_view_switched_to_has_a_section(self):
        page = self.html()
        views = set(re.findall(r'id="view-([A-Za-z0-9_-]+)"', page))
        switched = set(re.findall(r'state\.active === "([A-Za-z0-9_-]+)"',
                                  code_only()))
        switched |= set(re.findall(r'data-view="([A-Za-z0-9_-]+)"', page))

        self.assertTrue(views, "no views found on the page")
        self.assertEqual(sorted(switched - views), [])

    def test_every_api_path_called_by_name_has_a_route(self):
        """Literal calls only. A URL built by concatenation is invisible here -
        /api/filelists/bot/<nick> is one - so this is a floor, not a ceiling."""
        import webserver as _webserver

        with io.open(os.path.join(REPO_ROOT, "webserver.py"),
                     encoding="utf-8") as handle:
            server = handle.read()

        def flat(path):
            return re.sub(r"/+", "/", path.split("?")[0]).rstrip("/")

        routes = {flat(re.sub(r"<[^>]+>", "", rule))
                  for rule in re.findall(r'@app\.route\("([^"]+)"', server)}
        routes.discard("")
        called = {flat(c) for c in re.findall(
            r'(?:fetchJson|fetchJsonAllowingError|postJson|deleteJson)'
            r'\(\s*"([^"]+)"', code_only())}

        self.assertTrue(called, "no API calls found - has the pattern gone stale?")
        self.assertTrue(routes, "no routes found")
        for path in sorted(called):
            with self.subTest(path=path):
                self.assertTrue(
                    any(path == r or path.startswith(r + "/")
                        or r.startswith(path + "/") for r in routes),
                    "%s is called and no route serves it" % path)


if __name__ == "__main__":
    unittest.main()
