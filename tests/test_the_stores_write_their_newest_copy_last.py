"""#1273: a save that waited its turn wrote an older copy over a newer one.

db.save_dcc_queue() copied the queue BEFORE taking the disk lock. Two saves
that met at the lock could then land in the other order: the one that copied
first, and waited, wrote last. The file held a queue the freeze timer had
already erased and lacked the one queued in between, and a restart before the
next save kept it that way. The copy is now taken inside the lock, so the copies
reach the file in the order they were taken.

list_fetch._note_auto_attempt() and mark_seen() copied the fetched-lists
registry under its lock and wrote it after letting go. A list install that
finished in between wrote its new entry, and the older copy then landed on top
of it: the file named a list file the install had already removed. They now
write under the lock, as the install itself does.
"""

import json
import os
import threading
import unittest

from tests import support  # noqa: F401  (path setup)
from tests.support import DCCoreTestCase  # noqa: E402

import db  # noqa: E402
import defaults as config  # noqa: E402
import list_fetch  # noqa: E402


class ALockThatLetsSomethingHappenFirst:
    """Stands in for db._disk_lock. The first time a save asks for it,
    `meanwhile` runs before the lock is taken: what another thread does while
    this save waits its turn."""

    def __init__(self, real, meanwhile):
        self.real = real
        self.meanwhile = meanwhile

    def __enter__(self):
        meanwhile, self.meanwhile = self.meanwhile, None
        if meanwhile is not None:
            meanwhile()
        self.real.acquire()
        return self

    def __exit__(self, *exc):
        self.real.release()
        return False


class TheQueueFile(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        folder = self.make_temp_dir()
        self.shared = os.path.join(folder, "shared")
        os.makedirs(self.shared)
        self.set_config(FILE_DIRECTORY=self.shared)
        self.addCleanup(setattr, db, "DCC_QUEUE_FILE", db.DCC_QUEUE_FILE)
        db.DCC_QUEUE_FILE = os.path.join(folder, "dcc_queue.txt")
        before = dict(config.dcc_queue)
        self.addCleanup(lambda: (config.dcc_queue.clear(), config.dcc_queue.update(before)))
        config.dcc_queue.clear()

    def entry(self, name):
        return {"path": os.path.join(self.shared, name), "filename": name}

    def test_a_save_that_waited_for_the_lock_writes_the_queue_as_it_is_then(self):
        config.dcc_queue["alfa"] = [self.entry("Song One.mp3")]

        def meanwhile():
            # The freeze timer erases alfa's queue and bravo queues, while
            # this save waits behind another write.
            del config.dcc_queue["alfa"]
            config.dcc_queue["bravo"] = [self.entry("Song Two.mp3")]

        self.addCleanup(setattr, db, "_disk_lock", db._disk_lock)
        db._disk_lock = ALockThatLetsSomethingHappenFirst(db._disk_lock, meanwhile)

        db.save_dcc_queue()

        with open(db.DCC_QUEUE_FILE, encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(sorted(saved), ["bravo"],
                         "the file must hold the queue as it was when the write ran")
        self.assertEqual(saved["bravo"], [self.entry("Song Two.mp3")])

    def test_an_ordinary_save_still_leaves_out_an_emptied_queue(self):
        config.dcc_queue["alfa"] = [self.entry("Song One.mp3")]
        config.dcc_queue["bravo"] = []
        db.save_dcc_queue()
        with open(db.DCC_QUEUE_FILE, encoding="utf-8") as handle:
            self.assertEqual(sorted(json.load(handle)), ["alfa"])


class TheFetchedListsRegistry(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        folder = self.make_temp_dir()
        self.addCleanup(setattr, db, "FETCHED_BOT_LISTS_FILE", db.FETCHED_BOT_LISTS_FILE)
        db.FETCHED_BOT_LISTS_FILE = os.path.join(folder, "fetched_bot_lists.json")
        # A plain Lock of the test's own, so whether it is held can be asked
        # on every Python this runs on.
        self.lock = threading.Lock()
        self.set_config(fetched_bot_lists_lock=self.lock)
        store = config.fetched_bot_lists
        before = dict(store)
        self.addCleanup(lambda: (store.clear(), store.update(before)))
        store.clear()
        store["alfabot"] = {"list_path": os.path.join(folder, "alfabot", "alfabot-2026-01-01.txt"),
                            "fetched_at": 2000.0, "seen_at": 1000.0}
        self.held_while_saving = []
        real_save = db.save_fetched_bot_lists

        def save(registry):
            self.held_while_saving.append(self.lock.locked())
            return real_save(registry)

        self.addCleanup(setattr, db, "save_fetched_bot_lists", real_save)
        db.save_fetched_bot_lists = save

    def saved(self):
        with open(db.FETCHED_BOT_LISTS_FILE, encoding="utf-8") as handle:
            return json.load(handle)

    def test_the_sweeps_mark_is_written_under_the_registry_lock(self):
        self.assertIs(list_fetch._lock(), self.lock)
        list_fetch._note_auto_attempt("AlfaBot", 3000.0)
        self.assertEqual(self.held_while_saving, [True],
                         "written after the lock is let go, an install in between is overwritten")
        self.assertEqual(self.saved()["alfabot"]["last_attempt"], 3000.0)

    def test_opening_a_list_is_written_under_the_registry_lock(self):
        self.assertTrue(list_fetch.mark_seen("AlfaBot"))
        self.assertEqual(self.held_while_saving, [True],
                         "written after the lock is let go, an install in between is overwritten")
        self.assertGreater(self.saved()["alfabot"]["seen_at"], 2000.0)

    def test_nothing_is_written_for_a_bot_with_no_entry(self):
        list_fetch._note_auto_attempt("BravoBot", 3000.0)
        self.assertFalse(list_fetch.mark_seen("BravoBot"))
        self.assertEqual(self.held_while_saving, [])


if __name__ == "__main__":
    unittest.main()
