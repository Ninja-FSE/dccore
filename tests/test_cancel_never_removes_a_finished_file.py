"""The dashboard's Cancel never removes a file that finished meanwhile (#1046).

Cancel is offered on a request that has not started - pending, offered, or
queued at the other bot - and asks "Nothing has been downloaded yet." The table
can be seconds old and the confirm dialog stops it refreshing, so the transfer
could finish before OK was clicked. Cancel and Delete posted the same thing,
and the route took the now-complete row for a Delete and removed its file. The
mIRC window's Cancel already asked the server to refuse anything that is no
longer waiting; the dashboard's now does too.
"""

import io
import os
import shutil
import tempfile
import unittest

from tests import support  # noqa: F401  (path setup)

import adminchat  # noqa: E402
import defaults as config  # noqa: E402
import webserver  # noqa: E402
from tests.test_webserver import WEBUI_TEST_PASSWORD, log_in_test_client  # noqa: E402

REPO_ROOT = support.REPO_ROOT


@unittest.skipUnless(webserver.HAVE_FLASK, "Flask not installed; CI installs requirements-web.txt")
class TheRoute(support.DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-cancel-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        config.FETCHED_FILES_DIR = self.tmp
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(
            WEBUI_TEST_PASSWORD, iterations=1000))
        self.client = webserver.create_app().test_client()
        log_in_test_client(self.client)

    def put(self, state, stored=None):
        if stored:
            with open(os.path.join(self.tmp, stored), "w") as handle:
                handle.write("data")
        config.fetch_queue["rid"] = {
            "bot": "SomeBot", "filename": "Song.flac", "request_type": "file",
            "state": state, "requested_at": 1.0, "offered_at": 1.0,
            "bytes_received": 4 if stored else 0, "total_size": 4, "reason": "",
            "stored_filename": stored,
        }

    def test_a_cancel_that_finds_the_file_finished_is_refused_and_the_file_stays(self):
        self.put("complete", "rid_Song.flac")
        resp = self.client.post("/api/fetch/rid/delete", json={"only_waiting": True})
        self.assertEqual(resp.status_code, 409)
        self.assertIn("rid", config.fetch_queue)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "rid_Song.flac")))

    def test_a_cancel_of_a_waiting_request_still_lets_it_go(self):
        for state in ("pending", "offered", "queued"):
            with self.subTest(state=state):
                self.put(state)
                resp = self.client.post("/api/fetch/rid/delete", json={"only_waiting": True})
                self.assertEqual(resp.status_code, 200, resp.get_json())
                self.assertNotIn("rid", config.fetch_queue)

    def test_delete_still_removes_a_finished_file(self):
        self.put("complete", "rid_Song.flac")
        resp = self.client.post("/api/fetch/rid/delete", json={})
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "rid_Song.flac")))

    def test_only_a_real_true_asks_for_it(self):
        self.put("complete", "rid_Song.flac")
        resp = self.client.post("/api/fetch/rid/delete", json={"only_waiting": "no"})
        self.assertEqual(resp.status_code, 200)


class ThePage(unittest.TestCase):
    def test_cancel_says_it_means_only_a_waiting_request(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
            js = handle.read()
        at = js.index('var btn = evt.target.closest ? evt.target.closest(".fetch-delete-btn") : null;')
        handler = js[at:at + 2000]
        self.assertIn('var body = btn.dataset.pending ? { only_waiting: true } : {};', handler)
        self.assertIn('"/delete", body).then(', handler)

    def test_a_refused_cancel_redraws_the_list(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
            js = handle.read()
        at = js.index('var btn = evt.target.closest ? evt.target.closest(".fetch-delete-btn") : null;')
        self.assertIn("if (res.status === 409) { loadDownloads(); }", js[at:at + 2000])


if __name__ == "__main__":
    unittest.main()
