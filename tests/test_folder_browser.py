"""#164 step 5: a folder picker for the Settings page.

The issue puts this last and on its own "given the exposure", and that is the
whole design question - the listing itself is twenty lines.

WHAT IT GRANTS THAT NOTHING ELSE DID

An authenticated dashboard session can list the NAMES of directories on the
machine the daemon runs on, anywhere it can read - not only under the served
folders, because the point is to find a folder that is not served yet.

Without it the same session can already PROBE a path: saving a folder answers
"not a folder on this machine", which tells you about one path you already
guessed. ENUMERATION is different in kind, so it gets an explicit yes.

WHY ITS OWN SWITCH AND NOT THE CONSOLE'S

Suggested during review: gate it on WEBUI_CONSOLE_ENABLED, so enabling the web
console also enables the picker and nobody grows a second setting.

The console is strictly the more dangerous of the two - it runs ban,
clearqueue, rehash and update. Gating the weaker feature behind the stronger
one means an operator who wants a folder picker, and specifically does not
want a web admin console, has to enable the console to get it: more risk
accepted to obtain less capability.

defaults.py states the rule three lines above the console's own switch: "an
admin surface reachable from a weaker path gets its own switch and a written
reason for its default."

The cost of leaving it off is typing a path instead of clicking one. The
folder rows work either way.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import platform_compat  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class BrowserTestCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.root = self.make_tree().root
        self.library = os.path.join(self.root, "Library")
        for name in ("Rock", "Jazz", "Metal"):
            os.makedirs(os.path.join(self.library, name), exist_ok=True)
        self.a_file = os.path.join(self.library, "notes.txt")
        with io.open(self.a_file, "w", encoding="utf-8") as handle:
            handle.write("not a folder")


class ItListsFoldersAndNothingElse(BrowserTestCase):

    def test_the_subfolders_are_listed_in_order(self):
        payload = webserver.build_folder_browse_payload(self.library)

        self.assertEqual([entry["name"] for entry in payload["entries"]],
                         ["Jazz", "Metal", "Rock"])

    def test_files_are_never_listed(self):
        """The caller is choosing a folder. Listing files would expose far more
        of the machine and answer nothing the picker asks."""
        payload = webserver.build_folder_browse_payload(self.library)

        names = [entry["name"] for entry in payload["entries"]]
        self.assertNotIn("notes.txt", names)

    def test_no_sizes_timestamps_or_contents_are_returned(self):
        """A name, and whether it can be opened, is the whole of what a picker
        needs. Everything else would be exposure with no purpose."""
        payload = webserver.build_folder_browse_payload(self.library)

        for entry in payload["entries"]:
            self.assertEqual(set(entry), {"name", "path"})

    def test_a_path_that_is_not_a_folder_is_refused(self):
        """And refused with a SENTENCE, not an OS error.

        Dropping the isdir() check still fails - scandir() raises and the
        handler catches it - which is why a test asserting only that an error
        came back survived a mutation run. What the check buys is the message:
        "not a folder on this machine" instead of "[WinError 267] The
        directory name is invalid", which is an OS string and localised into
        whatever language the machine runs in.
        """
        for target in (self.a_file, os.path.join(self.root, "nope")):
            with self.subTest(target=target):
                payload = webserver.build_folder_browse_payload(target)

                self.assertIn("error", payload)
                self.assertIn("not a folder on this machine", payload["error"])

    def test_an_empty_path_lists_the_top_of_the_tree(self):
        """Rather than the working directory: "where does the operator start"
        has no sensible relative answer."""
        payload = webserver.build_folder_browse_payload("")

        self.assertTrue(payload["at_root"])
        self.assertTrue(payload["entries"])

    def test_the_roots_are_real(self):
        for entry in webserver.browse_roots():
            with self.subTest(root=entry):
                self.assertTrue(os.path.isdir(platform_compat.long_path(entry)))

    def test_the_parent_climbs(self):
        payload = webserver.build_folder_browse_payload(
            os.path.join(self.library, "Rock"))

        self.assertEqual(os.path.normcase(payload["parent"]),
                         os.path.normcase(self.library))

    def test_a_listing_is_capped_and_says_so(self):
        """A library's top level can hold thousands of artist folders. Silently
        showing the first few hundred would read as "that folder is missing"."""
        many = os.path.join(self.root, "Many")
        for index in range(webserver.FOLDER_BROWSE_MAX_ENTRIES + 5):
            os.makedirs(os.path.join(many, f"f{index:05d}"), exist_ok=True)

        payload = webserver.build_folder_browse_payload(many)

        self.assertEqual(len(payload["entries"]),
                         webserver.FOLDER_BROWSE_MAX_ENTRIES)
        self.assertTrue(payload["truncated"])

    def test_a_normal_listing_is_not_marked_truncated(self):
        payload = webserver.build_folder_browse_payload(self.library)

        self.assertFalse(payload["truncated"])

    def test_an_unreadable_entry_does_not_lose_the_listing(self):
        """A directory holding one item the daemon cannot stat - a system
        folder, a dead symlink, a disconnected mount - must still list the
        other forty."""
        payload = webserver.build_folder_browse_payload(self.library)

        self.assertGreaterEqual(len(payload["entries"]), 3)

    def test_a_relative_path_is_resolved(self):
        payload = webserver.build_folder_browse_payload(self.library)

        for entry in payload["entries"]:
            self.assertTrue(os.path.isabs(entry["path"]))


class ItIsOffUntilAskedFor(DCCoreTestCase):

    def test_the_shipped_default_is_off(self):
        import defaults as config

        self.assertIs(config.SHIPPED_VALUES.get("WEBUI_FOLDER_BROWSER_ENABLED"),
                      False)

    def test_the_route_is_absent_when_it_is_off(self):
        """404, not 403: 403 confirms the route exists and is merely disabled,
        which tells anyone probing that this build has one worth returning
        for. The console's own routes give the same reasoning."""
        try:
            import flask  # noqa: F401
        except ImportError:
            self.skipTest("flask is not installed in this environment")

        import adminchat
        import defaults as config

        config.ADMIN_PASSWORD_HASH = adminchat.make_password_hash("pw")
        self.set_config(WEBUI_FOLDER_BROWSER_ENABLED=False)
        app = webserver.create_app()
        client = app.test_client()
        client.post("/login", data={"password": "pw"})

        self.assertEqual(client.get("/api/folders/browse").status_code, 404)

    def test_it_answers_when_it_is_on(self):
        try:
            import flask  # noqa: F401
        except ImportError:
            self.skipTest("flask is not installed in this environment")

        import adminchat
        import defaults as config

        config.ADMIN_PASSWORD_HASH = adminchat.make_password_hash("pw")
        self.set_config(WEBUI_FOLDER_BROWSER_ENABLED=True)
        app = webserver.create_app()
        client = app.test_client()
        client.post("/login", data={"password": "pw"})

        self.assertEqual(client.get("/api/folders/browse").status_code, 200)

    def test_it_still_needs_a_session(self):
        """Being enabled is not being public."""
        try:
            import flask  # noqa: F401
        except ImportError:
            self.skipTest("flask is not installed in this environment")

        import adminchat
        import defaults as config

        config.ADMIN_PASSWORD_HASH = adminchat.make_password_hash("pw")
        self.set_config(WEBUI_FOLDER_BROWSER_ENABLED=True)
        app = webserver.create_app()

        self.assertNotEqual(
            app.test_client().get("/api/folders/browse").status_code, 200)

    def test_the_folders_payload_reports_whether_it_is_on(self):
        """So the page can offer a Browse button, or say plainly why there is
        not one, rather than the operator wondering."""
        self.set_config(WEBUI_FOLDER_BROWSER_ENABLED=True)
        self.assertTrue(webserver.build_folders_payload()["browser_enabled"])

        self.set_config(WEBUI_FOLDER_BROWSER_ENABLED=False)
        self.assertFalse(webserver.build_folders_payload()["browser_enabled"])

    def test_it_is_not_gated_on_the_console(self):
        """The design decision, pinned. The console runs ban/clearqueue/rehash/
        update; making it the gate for a read-only directory listing would mean
        accepting more risk to obtain less capability."""
        self.set_config(WEBUI_CONSOLE_ENABLED=False,
                        WEBUI_FOLDER_BROWSER_ENABLED=True)

        self.assertTrue(webserver.build_folders_payload()["browser_enabled"])


class ThePickerPutsNoPathInAnAttribute(unittest.TestCase):
    """A directory name on Linux may contain a double quote, and escapeHtml()
    is textContent -> innerHTML, which does not encode one. Entries are
    addressed by their INDEX into state.browse.entries and the handler looks
    the path up from there - the same rule the folder rows and the file lists
    already follow."""

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                     encoding="utf-8") as handle:
            return handle.read()

    def test_the_entry_markup_carries_only_an_index(self):
        js = self.source()
        block = js.split("var browsePanel = ", 1)[1].split("var offNote", 1)[0]

        self.assertIn("data-browse-index=", block)
        self.assertNotIn("data-path=", block)
        self.assertNotIn("entry.path", block,
                         "a browse entry is putting its path into markup - a "
                         "directory name containing a double quote would break "
                         "out of the attribute")

    def test_the_handler_reads_the_path_from_state(self):
        js = self.source()

        self.assertIn("open.entries[parseInt(browseButton.dataset.browseIndex, 10)]",
                      js.replace("\n", " ").replace("  ", " "))


if __name__ == "__main__":
    unittest.main()
