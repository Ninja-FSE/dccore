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

import io
import os
import shutil
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402

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


class TheRehashCallsIt(unittest.TestCase):
    """handle_rehash_request() reloads half the daemon and cannot be run
    here - see test_rehash_config_window.py, and the same reasoning as
    test_a_rehash_keeps_the_interlocks_of_a_running_pack.py's own wiring
    class. The wiring is read instead: the call is there, and it is after
    the reload rather than before it, where the reload would undo it."""

    def rehash(self):
        with io.open(os.path.join(REPO_ROOT, "commands.py"), encoding="utf-8") as handle:
            return handle.read().split("def handle_rehash_request(", 1)[1]

    def test_the_rehash_forgets_the_lookups(self):
        self.assertIn("forget_library_lookups()", self.rehash())

    def test_and_does_so_after_the_reload(self):
        body = self.rehash()
        # Asserted here too, not just in the test above: index() below would
        # otherwise raise ValueError and report this as an error rather than
        # as the failure it is.
        self.assertIn("forget_library_lookups()", body)
        reload_at = body.index("\n        reload_modules_in_order()\n")

        self.assertLess(reload_at, body.index("forget_library_lookups()"),
                        "forgotten before the reload is forgotten too early: "
                        "the requests that arrive during the reload would "
                        "refill them from the old configuration")


if __name__ == "__main__":
    unittest.main()
