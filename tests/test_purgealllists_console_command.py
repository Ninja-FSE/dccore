"""`purgealllists confirm` (#1260): the console command for webserver.
build_purge_all_fetched_lists_result() - see tests/test_purge_all_fetched_
lists.py for that function's own behaviour. This only checks the command's
own plumbing: it asks for "confirm" first, the same reason `shutdown` asks
for "now", and reports what the route answered.
"""

import os
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class FakeSession:
    nick = "web:test"

    def __init__(self):
        self.lines = []

    def send(self, text=""):
        self.lines.append(str(text))


class ThePurgeAllListsCommand(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.session = FakeSession()
        patch = mock.patch.object(
            webserver, "build_purge_all_fetched_lists_result",
            return_value=(200, {"purged": ["SomeBot"], "count": 1, "skipped_in_flight": []}))
        self.purge = patch.start()
        self.addCleanup(patch.stop)

    def test_without_confirm_it_asks_first_and_does_nothing(self):
        adminchat._cmd_purgealllists(self.session, "")

        self.purge.assert_not_called()
        self.assertIn("confirm", self.session.lines[-1].lower())

    def test_a_different_word_also_asks_first(self):
        adminchat._cmd_purgealllists(self.session, "yes")

        self.purge.assert_not_called()

    def test_confirm_runs_it_and_reports_the_count(self):
        adminchat._cmd_purgealllists(self.session, "confirm")

        self.purge.assert_called_once()
        self.assertIn("Forgot 1", self.session.lines[-1])

    def test_confirm_is_not_case_sensitive(self):
        adminchat._cmd_purgealllists(self.session, "CONFIRM")

        self.purge.assert_called_once()

    def test_a_skipped_in_flight_bot_is_mentioned(self):
        self.purge.return_value = (200, {"purged": [], "count": 0, "skipped_in_flight": ["BusyBot"]})

        adminchat._cmd_purgealllists(self.session, "confirm")

        self.assertIn("1 left alone", self.session.lines[-1])

    def test_it_is_a_registered_command(self):
        self.assertIn("purgealllists", adminchat.COMMANDS)
        self.assertIs(adminchat.COMMANDS["purgealllists"][0], adminchat._cmd_purgealllists)


if __name__ == "__main__":
    unittest.main()
