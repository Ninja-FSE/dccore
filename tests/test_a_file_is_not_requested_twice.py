"""A file already asked for from a bot is not asked for again (#1218).

enqueue_fetch() refused a second outstanding "list"/"folder" row for a bot but
let any number of identical "file" rows in: a bulk paste with a repeated line,
a second search that showed the same result, or a double click each made
another row, and the dispatcher sent the bot one `!bot file` per row - the
bot's own log then shows the same request several times in a few seconds.
"""

import unittest

from tests import support  # noqa: F401  (path setup)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class AskingTwice(DCCoreTestCase):

    def test_the_same_file_from_the_same_bot_is_one_row(self):
        first = dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
        second = dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
        self.assertEqual(first, second)
        self.assertEqual(len(config.fetch_queue), 1)

    def test_the_bots_case_and_the_space_or_underscore_do_not_make_it_another(self):
        first = dcc_fetch.enqueue_fetch("GoodBot", "Some Song.flac")
        second = dcc_fetch.enqueue_fetch("goodbot", "some_song.FLAC")
        self.assertEqual(first, second)
        self.assertEqual(len(config.fetch_queue), 1)

    def test_a_size_suffix_copied_from_a_list_row_does_not_make_it_another(self):
        first = dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
        second = dcc_fetch.enqueue_fetch("goodbot", "Song.flac  ::INFO:: 79.53MB")
        self.assertEqual(first, second)

    def test_the_same_file_from_another_bot_is_its_own_request(self):
        dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
        dcc_fetch.enqueue_fetch("otherbot", "Song.flac")
        self.assertEqual(len(config.fetch_queue), 2)

    def test_another_file_from_the_same_bot_is_its_own_request(self):
        dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
        dcc_fetch.enqueue_fetch("goodbot", "Other.flac")
        self.assertEqual(len(config.fetch_queue), 2)

    def test_every_state_before_it_ends_counts_as_already_asked(self):
        for state in dcc_fetch._UNRESOLVED_FETCH_STATES:
            config.fetch_queue.clear()
            first = dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
            config.fetch_queue[first]["state"] = state
            self.assertEqual(dcc_fetch.enqueue_fetch("goodbot", "Song.flac"), first, state)
            self.assertEqual(len(config.fetch_queue), 1, state)

    def test_a_finished_or_failed_request_can_be_made_again(self):
        for state in ("complete", "failed"):
            config.fetch_queue.clear()
            first = dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
            config.fetch_queue[first]["state"] = state
            second = dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
            self.assertNotEqual(first, second, state)
            self.assertEqual(len(config.fetch_queue), 2, state)

    def test_a_request_that_is_in_the_queue_does_not_use_up_room(self):
        for _ in range(3):
            dcc_fetch.enqueue_fetch("goodbot", "Song.flac")
        self.assertEqual(dcc_fetch.count_unresolved_fetches(config.fetch_queue), 1)


class OnTheDashboard(DCCoreTestCase):

    def test_a_repeated_line_in_one_paste_is_queued_once_and_said_so(self):
        status, result = webserver.build_fetch_enqueue_result([
            {"bot": "goodbot", "filename": "Song.flac"},
            {"bot": "goodbot", "filename": "Song.flac"},
            {"bot": "goodbot", "filename": "Other.flac"},
        ])
        self.assertEqual(status, 200)
        self.assertEqual(len(result["created"]), 2)
        self.assertEqual(len(config.fetch_queue), 2)
        self.assertEqual(len(result["errors"]), 1)
        self.assertIn("already", result["errors"][0]["error"].lower())

    def test_asking_again_while_it_waits_adds_nothing_and_says_why(self):
        webserver.build_fetch_enqueue_result({"bot": "goodbot", "filename": "Song.flac"})
        status, result = webserver.build_fetch_enqueue_result({"bot": "goodbot", "filename": "Song.flac"})
        self.assertEqual(status, 400)
        self.assertEqual(result["created"], [])
        self.assertIn("already", result["errors"][0]["error"].lower())
        self.assertEqual(len(config.fetch_queue), 1)


if __name__ == "__main__":
    unittest.main()
