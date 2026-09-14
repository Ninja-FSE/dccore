"""The last four dashboard findings of the full-program audit.

1. A FETCHED FILE WITH A LONG REMOTE-CHOSEN NAME COULD NOT BE DOWNLOADED.

   dcc_fetch fits a stored name to MAX_NAME_BYTES (255) and touches every path
   through platform_compat.long_path(). The dashboard's download route was the
   one that did not, so on Windows the file was written, marked "complete",
   listed in the UI - and its Download button answered 404 for ever, while the
   file sat there the whole time.

   Handing send_from_directory() the WRAPPED directory does not fix it:
   werkzeug's safe_join() joins with a FORWARD SLASH, and a \\\\?\\ path is the
   one kind Windows will not accept those in. The path is built here instead,
   with a backslash, wrapped after - and the containment safe_join() provided
   is kept explicitly with dcc.is_safe_path(), which additionally resolves
   symlinks.

2. A SERVED-FOLDER PATH HAD NO LENGTH CAP.

   apply_folder_changes() bounded the NUMBER of folders and not the length of
   any one path, and library.problems() embeds each offending path verbatim in
   up to two messages per entry - so the 400 response was roughly twice the
   request that caused it. Every other web input in the module is bounded.

3. "UP" FROM A LOWERCASE DRIVE ROOT LEFT THE MACHINE ROOT.

   browse_roots() builds its entries from the uppercase letters A-Z, and
   os.path.abspath() preserves whatever case the caller sent - so "c:\\" missed
   the root list and the parent fell through to ntpath.dirname("c:"), which is
   "c:": a DRIVE-RELATIVE path meaning "the current directory on C:". Clicking
   Up from a lowercase drive root browsed the daemon's own working directory.

4. A DEAD "GET FOLDER AS .RAR" BUTTON.

   folderHeadingHtml() rendered it for every group of a foreign bot's list,
   including the unnamed one that holds any rows sitting above the first
   folder heading. requestFolderRar() drops the click on `if (!bot || !folder)`,
   so the button was there, clickable, and did nothing at all - no request, no
   message.
"""

import io
import json
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


def needs_flask(test):
    try:
        import flask  # noqa: F401
    except ImportError:
        raise unittest.SkipTest("flask is not installed in this environment")
    return test


class ALongFetchedNameCanStillBeDownloaded(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        needs_flask(self)
        import adminchat
        import defaults as config

        self.fetched = os.path.join(self.make_tree().root, "fetched")
        os.makedirs(self.fetched, exist_ok=True)
        self.set_config(FETCHED_FILES_DIR=self.fetched)
        config.ADMIN_PASSWORD_HASH = adminchat.make_password_hash("pw")
        self.app = webserver.create_app()
        self.client = self.app.test_client()
        self.client.post("/login", data={"password": "pw"})

    def store(self, stored_name, body=b"payload-bytes"):
        target = platform_compat.long_path(
            os.path.join(self.fetched, stored_name))
        with io.open(target, "wb") as handle:
            handle.write(body)
        self.set_config(fetch_queue={"req1": {
            "state": "complete", "stored_filename": stored_name,
            "filename": "Song.mp3"}})

    def test_a_name_at_the_fetch_limit_downloads(self):
        """247 characters is what a remote bot can produce within
        dcc_fetch.MAX_NAME_BYTES, and is past MAX_PATH once the directory is
        in front of it."""
        self.store("abcdef123456_" + "a" * 230 + ".mp3")

        response = self.client.get("/api/fetch/req1/download")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_data(), b"payload-bytes")

    def test_an_ordinary_name_still_downloads(self):
        """Control."""
        self.store("abcdef123456_Song.mp3")

        response = self.client.get("/api/fetch/req1/download")

        self.assertEqual(response.status_code, 200)

    def test_a_file_that_is_gone_is_a_clean_404(self):
        self.store("abcdef123456_Song.mp3")
        os.unlink(os.path.join(self.fetched, "abcdef123456_Song.mp3"))

        response = self.client.get("/api/fetch/req1/download")

        self.assertEqual(response.status_code, 404)

    def test_a_stored_name_that_escapes_is_refused(self):
        """The containment safe_join() used to provide, kept explicitly.

        The escaping name must point at a file that REALLY EXISTS, or the
        os.path.isfile() check below refuses it anyway and the test passes
        with the containment deleted - which is exactly what a mutation run
        showed. Not reachable through dcc_fetch, which validates before
        storing; this is the guard that keeps it unreachable.
        """
        outside = os.path.join(os.path.dirname(self.fetched), "secret.txt")
        with io.open(outside, "wb") as handle:
            handle.write(b"not-for-the-dashboard")
        self.assertTrue(os.path.isfile(outside))

        self.set_config(fetch_queue={"req1": {
            "state": "complete",
            "stored_filename": os.path.join("..", "secret.txt"),
            "filename": "secret.txt"}})

        response = self.client.get("/api/fetch/req1/download")

        self.assertEqual(response.status_code, 404)
        self.assertNotIn(b"not-for-the-dashboard", response.get_data())

    def test_it_does_not_use_send_from_directory(self):
        """werkzeug's safe_join() joins with a forward slash, which a \\\\?\\
        path will not accept - so wrapping the directory and keeping
        send_from_directory() looks like a fix and is not."""
        with io.open(os.path.join(REPO_ROOT, "webserver.py"),
                     encoding="utf-8") as handle:
            source = handle.read()
        route = source.split("def api_fetch_download(", 1)[1]
        route = route.split("@app.route", 1)[0]
        # Comments out first. This route's own comment explains at length why
        # send_from_directory() is NOT used, so a scan that reads prose finds
        # the name and reports the opposite of the truth. Four guards written
        # during this audit matched a comment instead of code before the habit
        # stuck.
        code = "\n".join(line.split("#", 1)[0] for line in route.splitlines())

        self.assertNotIn("send_from_directory(", code)
        self.assertIn("platform_compat.long_path(", code)


class AFolderPathIsBounded(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(LIBRARY_FOLDERS_FILE=os.path.join(
            self.make_tree().root, "folders.json"))

    def test_an_absurd_path_is_refused(self):
        status, result = webserver.apply_folder_changes(
            {"folders": [{"name": "X", "path": "C:" + os.sep + "a" * 200000}]})

        self.assertEqual(status, 400)
        self.assertIn("longer than", result["error"])

    def test_the_refusal_does_not_echo_the_path_back(self):
        """library.problems() embeds each offending path verbatim in up to two
        messages per entry, so without the cap the 400 was twice the
        request."""
        status, result = webserver.apply_folder_changes(
            {"folders": [{"name": "X", "path": "C:" + os.sep + "a" * 200000}]})

        self.assertLess(len(json.dumps(result)), 500)

    def test_an_absurd_label_is_refused_too(self):
        status, _result = webserver.apply_folder_changes(
            {"folders": [{"name": "n" * 200000, "path": "C:" + os.sep}]})

        self.assertEqual(status, 400)

    def test_a_real_path_is_unaffected(self):
        """Control. The cap must bound the absurd without touching the
        possible."""
        root = self.make_tree().root
        deep = os.path.join(root, *(["folder"] * 20))
        os.makedirs(deep, exist_ok=True)

        status, _result = webserver.apply_folder_changes(
            {"folders": [{"name": "Deep", "path": deep}]})

        self.assertEqual(status, 200)


class ClimbingFromADriveRootReachesTheRootList(unittest.TestCase):

    def setUp(self):
        if os.name != "nt":
            self.skipTest("drive roots are a Windows shape")

    def drive(self):
        return os.path.splitdrive(os.path.abspath(REPO_ROOT))[0]

    def test_every_spelling_of_the_drive_root_climbs_to_the_root_list(self):
        for spelling in (self.drive() + os.sep,
                         self.drive().lower() + os.sep,
                         self.drive().upper() + os.sep):
            with self.subTest(spelling=spelling):
                payload = webserver.build_folder_browse_payload(spelling)

                self.assertEqual(payload["parent"], "",
                                 "Up from a drive root must offer the machine "
                                 "root, not a drive-relative path")

    def test_an_ordinary_folder_still_climbs_to_its_parent(self):
        """Control."""
        payload = webserver.build_folder_browse_payload(REPO_ROOT)

        self.assertEqual(os.path.normcase(payload["parent"]),
                         os.path.normcase(os.path.dirname(REPO_ROOT)))


class TheRarButtonIsOnlyOnFoldersThatHaveOne(unittest.TestCase):

    def test_the_folder_heading_offers_to_pack_nothing(self):
        """This required a `&& !!group.folder` gate on the heading's own .rar
        button, because requestFolderRar() drops the click on
        `if (!bot || !folder)` and a button that does nothing says nothing.

        The heading has no such button at all now. It could only ever guess:
        measured against one live registry, 2 of 51 known bots publish a RAR
        list, so the button was wrong for the other 49 - and a click sent
        "!nick !rar <folder>" into the channel and held a fetch slot for half
        an hour waiting for a reply that was never coming.

        The question is per FOLDER, not per bot, and the rows of a bot's RAR
        list answer exactly that - so the button lives on the row carrying the
        request line. Read out of the source: nothing here executes
        JavaScript."""
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                     encoding="utf-8") as handle:
            source = handle.read()
        heading = source.split("function folderHeadingHtml(", 1)[1]
        heading = heading.split("\n    function ", 1)[0]

        self.assertNotIn("folder-rar-btn", heading,
                         "the folder heading offers to pack a folder again, "
                         "which it cannot know that bot will pack")
        self.assertIn("folder-rar-btn",
                      source.split("function folderFilesHtml(", 1)[1][:4000],
                      "and the row that DOES carry the request line has lost "
                      "its button")


if __name__ == "__main__":
    unittest.main()
