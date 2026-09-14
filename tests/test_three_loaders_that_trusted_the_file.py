"""State files are hand-editable by design, and two loaders trusted them.

`docs/` tells operators they can edit these files. Every loader in `db.py`
therefore filters row by row, so one bad entry costs that entry and not the
rest - except two:

  * `load_dcc_queue()` checked that the top level was a dict and then
    `update()`d the file's contents wholesale, so a value that was not a list,
    or a row that was not a dict, reached `config.dcc_queue` and failed later
    on a dispatch thread, far from the file that caused it (#450);
  * `load_notices()` kept any row that merely HAD an `id`, while the `seen_id`
    marker two lines below was already coerced with a guarded `int()` - so a
    hand-edited `"id": "first"` survived the loader and raised wherever ids
    are compared, taking the whole panel out rather than the one row (#451).

The third is `save_dcc_queue()` walking the live dict (#452). See that class
for why its guard is structural rather than behavioural.
"""

import io
import json
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class OneBadQueueEntryCostsOnlyItself(DCCoreTestCase):

    def write_queue(self, payload):
        folder = tempfile.mkdtemp()
        db.DCC_QUEUE_FILE = os.path.join(folder, "dcc_queue.txt")
        with io.open(db.DCC_QUEUE_FILE, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload))
        config.dcc_queue.clear()

    def test_a_value_that_is_not_a_list_does_not_take_the_others(self):
        self.write_queue({"good": [{"file": "a.mp3"}], "broken": "oops"})

        db.load_dcc_queue()

        self.assertIn("good", config.dcc_queue)
        self.assertNotIn("broken", config.dcc_queue)

    def test_a_row_that_is_not_a_dict_is_dropped_and_its_neighbours_kept(self):
        self.write_queue({"someuser": [{"file": "a.mp3"}, "junk", {"file": "b.mp3"}]})

        db.load_dcc_queue()

        self.assertEqual(len(config.dcc_queue["someuser"]), 2)

    def test_a_user_left_with_nothing_usable_is_not_carried_as_an_empty_queue(self):
        """An empty list would read as "this person has a queue" everywhere
        that tests for presence rather than length."""
        self.write_queue({"someuser": ["junk", 7]})

        db.load_dcc_queue()

        self.assertNotIn("someuser", config.dcc_queue)

    def test_a_wholly_good_file_is_untouched(self):
        self.write_queue({"a": [{"file": "1"}], "b": [{"file": "2"}, {"file": "3"}]})

        db.load_dcc_queue()

        self.assertEqual({k: len(v) for k, v in config.dcc_queue.items()},
                         {"a": 1, "b": 2})


class ANoticeIdHasToBeANumber(DCCoreTestCase):

    def write_notices(self, rows):
        folder = tempfile.mkdtemp()
        db.NOTICES_FILE = os.path.join(folder, "notices.json")
        with io.open(db.NOTICES_FILE, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"notices": rows, "state": {"seen_id": 0}}))

    def test_a_non_numeric_id_is_dropped_rather_than_kept(self):
        """It used to survive the loader and raise wherever ids are compared,
        which costs the panel rather than the row."""
        self.write_notices([{"id": 1, "text": "ok"}, {"id": "first", "text": "bad"}])

        rows, _state = db.load_notices()

        self.assertEqual([row["id"] for row in rows], [1])

    def test_a_numeric_string_is_coerced_rather_than_discarded(self):
        """Hand-edited JSON quotes numbers all the time, and "3" means 3."""
        self.write_notices([{"id": "3", "text": "coercible"}])

        rows, _state = db.load_notices()

        self.assertEqual(rows[0]["id"], 3)

    def test_every_surviving_id_is_an_int(self):
        """The property the comparisons downstream depend on."""
        self.write_notices([{"id": 1}, {"id": "2"}, {"id": None}, {"id": "x"}])

        rows, _state = db.load_notices()

        self.assertTrue(rows)
        for row in rows:
            self.assertIsInstance(row["id"], int)


class TheSaveWalksACopy(unittest.TestCase):
    """#452, guarded by reading the source rather than by racing it.

    I could not reproduce the failure: sixty concurrent saves against a
    queue being mutated by another thread raised nothing, with the fix and
    without it. The prune already snapshotted its keys with list(); only the
    final dict comprehension over .items() was ever exposed, and that window
    is narrow enough that a stress test does not reliably land in it.

    So this asserts the shape instead. The shape is the same one #432 settled
    on for get_total_queued_count(), and for the same reason: queue_lock
    cannot be taken here, because five of the six callers in dcc.py are
    already holding it and it is not reentrant.
    """

    @staticmethod
    def body():
        with io.open(os.path.join(REPO_ROOT, "db.py"), encoding="utf-8") as handle:
            source = handle.read()
        body = source.split("def save_dcc_queue", 1)[1]
        return body.split("\ndef ", 1)[0]

    def test_it_takes_a_copy_before_walking(self):
        self.assertIn("dict(config.dcc_queue)", self.body())

    def test_neither_loop_walks_the_live_dict(self):
        body = self.body()
        after_copy = body.split("dict(config.dcc_queue)", 1)[1]

        self.assertNotIn("config.dcc_queue.items()", after_copy,
                         "the serialising comprehension walks the live dict "
                         "again - that is the one that could raise")
        self.assertNotIn("config.dcc_queue.keys()", after_copy)

    def test_it_does_not_take_the_queue_lock(self):
        """Five of the six callers in dcc.py already hold it, and it is a
        plain threading.Lock - locking here deadlocks the request path.

        Asserted on the ACQUISITION, not on the word: the comment in the
        function explains why the lock cannot be taken, so a test searching
        for "queue_lock" matches the explanation and fails on a correct
        implementation. That trap has caught me three times today.
        """
        import re

        body = self.body()
        code = re.sub(chr(35) + "[^" + chr(10) + "]*", "", body)
        code = re.sub(r'"""..*?"""', "", code, flags=re.S)

        self.assertNotRegex(code, r"with\s+[\w.]*queue_lock",
                            "save_dcc_queue acquires queue_lock, which its "
                            "own callers already hold - that deadlocks")
