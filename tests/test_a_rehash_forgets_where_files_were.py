"""The lookup memories are dropped when the library is reconfigured (#886),
and checked against the CURRENT configuration wherever they are used (#901).

#886 gave the request path three memories so a pasted batch costs one
scan instead of nine. Each is a hint that re-checks the file on disk
before it is trusted, which is what makes a moved or deleted file cost a
stale check rather than a wrong answer.

That existence check cannot catch every way the library changes, though:
a path remembered under a folder the operator has just REMOVED is still
there on disk. #889 made a rehash forget the memories outright - but the
dashboard's Folders and Lists pages change the library WITHOUT a rehash
(webserver.py's apply_folder_changes()/apply_list_changes() save and
return; library.folders() reads the file on every call), so a memory made
just before such a save outlived the root it came from until either
LOOKUP_HIT_TTL_SECONDS passed or a !rehash happened to run.

#901 closes that gap at the point of use: a remembered path is checked
against search_roots - the roots this very request just resolved, not
whatever they were when the memory was made - before it is trusted, the
same is_safe_path() check the request makes on any other path afterwards.
Outside them, the request falls through to the folder memory and then the
scan instead of being refused. #889's !rehash call stays: it still frees
the memory outright, which this does not replace - a scan still in flight
across a rehash records its hit after the forget, and this is what keeps
THAT entry honest too, along with every other way the roots can move.

A rebuild (!update) needs no such call: it changes the lists, not where
the files are, and anything it does move fails the on-disk check.
"""

import os
import shutil
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402

from tests.support import rehash_with_the_reload_stood_in  # noqa: E402
from tests.test_an_unknown_filename_does_not_scan_the_library_unbounded import LookupBase  # noqa: E402


class ARememberedPathOutlivesTheRootItCameFrom(LookupBase):
    """Driven through the real request path, with the memories left in
    place (no forget, no rehash) - #901's own case: a dashboard save that
    changes the library without either."""

    def setUp(self):
        super().setUp()
        # What an operator moving their library to another mount actually
        # has: the same album, complete, under a different root. The old
        # one stays on disk - that is the whole point, and why the hint's
        # own existence check cannot catch this.
        self.moved_root = os.path.join(self.tree.root, "moved")
        shutil.copytree(self.tree.music, self.moved_root)

    def a_track(self):
        return os.path.basename(self.tree.tracks[0])

    def point_the_library_at(self, root):
        """What a dashboard Folders save does to search_roots - live on the
        very next request, no rehash and no reload involved (#901)."""
        config.FILE_DIRECTORY = root

    def test_a_path_from_a_root_that_is_gone_is_served_from_the_new_one(self):
        """#901: checked against search_roots at the point of use, so this
        is served even with the memory still in place and no !rehash run -
        the dashboard case the old behaviour (asserted below) missed."""
        name = self.a_track()
        self.ask(name)
        self.assertEqual(self.errors(), [], "the first request should have been served")

        self.point_the_library_at(self.moved_root)
        self.ask(name)

        self.assertEqual(self.errors(), [],
                         "the remembered path, checked against the new roots, "
                         "should have been passed over for the new one")

    def test_forgetting_them_also_still_serves_it(self):
        """#889's !rehash call is not made redundant by #901 - it still
        frees the memory outright, which #901 does not replace."""
        name = self.a_track()
        self.ask(name)
        self.assertEqual(self.errors(), [])

        self.point_the_library_at(self.moved_root)
        dcc.forget_library_lookups()
        self.ask(name)

        self.assertEqual(self.errors(), [],
                         "the file is in the new root and was still refused")


class TheMemoriesAreReallyGone(LookupBase):
    """forget_library_lookups() against all three, named one at a time, so
    a memory added later without a line in that function is caught here."""

    def test_every_memory_is_dropped(self):
        self.ask(self.tree.tracks and os.path.basename(self.tree.tracks[0]))
        self.ask("Nothing By This Name.flac")
        self.assertTrue(dcc._lookup_hits, "nothing was remembered to drop")
        self.assertTrue(dcc._lookup_folders)
        self.assertTrue(dcc._lookup_misses)

        dcc.forget_library_lookups()

        self.assertEqual(dcc._lookup_hits, {})
        self.assertEqual(dcc._lookup_folders, {})
        self.assertEqual(dcc._lookup_misses, {})


class TheRehashCallsIt(LookupBase):
    """The real handle_rehash_request(), with only its module reload stood in
    for (tests.support.rehash_with_the_reload_stood_in; this was read out of
    commands.py, #1469): the memories are gone afterwards, and gone even when
    requests arrive during the reload, which is why the forget comes after
    it - forgotten before the reload, the requests that arrive during it
    would refill them from the old configuration."""

    def setUp(self):
        super().setUp()
        # A found track claims a slot whose send never runs here (the
        # fixture's threads are recorded, not started), so the quiesce would
        # wait out its whole bound for it.
        self.set_config(REHASH_TRANSFER_WAIT=0)

    def remember_something(self):
        self.ask(os.path.basename(self.tree.tracks[0]))
        self.ask("Nothing By This Name.flac")

    def memories(self):
        return dcc._lookup_hits, dcc._lookup_folders, dcc._lookup_misses

    def test_the_rehash_forgets_the_lookups(self):
        self.remember_something()
        self.assertTrue(all(self.memories()), "the control: nothing was remembered to drop")

        self.assertIs(rehash_with_the_reload_stood_in(self, lambda *_a, **_k: []), True)

        self.assertEqual(self.memories(), ({}, {}, {}))

    def test_and_does_so_after_the_reload(self):
        filled_during_the_reload = []

        def reload_while_requests_arrive(*_args, **_kwargs):
            self.remember_something()
            filled_during_the_reload.append(all(self.memories()))
            return []

        self.assertIs(rehash_with_the_reload_stood_in(self, reload_while_requests_arrive), True)

        self.assertEqual(filled_during_the_reload, [True], "the control: the requests remembered nothing")
        self.assertEqual(self.memories(), ({}, {}, {}),
                         "what was learned during the reload outlived the rehash")


if __name__ == "__main__":
    unittest.main()
