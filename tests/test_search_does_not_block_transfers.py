"""A search must not refuse everyone else's file requests.

THE DEFECT (#214)

`config.search_inprogress` meant two different things. `!update` set it to say
"a rebuild is running, the list files are being replaced underneath you", and
`execute_search()` set the same flag for the few seconds it spent READING the
list.

`dcc.py`'s maintenance gate refused every incoming file request while that flag
was set, and told the user:

    MasterList is currently rebuilding. File requests temporarily paused.

Which, during a search, is false in every particular. Nothing was rebuilding;
somebody had typed @find. On a busy bot that is a steady stream of refusals
with a misleading explanation, and the operator goes looking at the list build.

THE FIX

`update_inprogress` is the flag that actually means "a rebuild is running" -
`!update` sets it unconditionally, while it only sets `search_inprogress` when
PAUSE_ON_UPDATE is on. Both maintenance gates now read it, so a real rebuild
refuses exactly what it refused before and a search refuses nothing.

`search_inprogress` keeps its remaining job: one search at a time.

AND ONE THING THE FIX SURFACED

The "another search is already running" branch printed to the console and
returned, sending the user nothing. It was unreachable with PAUSE_ON_UPDATE on,
because the rebuild branch returned first on the identical flag - so nobody had
ever hit it. Separating the flags makes it the branch a second searcher
actually lands on, so it needed a reply, and an accurate one.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402

from tests.support import DCCoreTestCase, RecordingSocket  # noqa: E402


class SearchCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        os.makedirs(self.tree.lists, exist_ok=True)
        self.track = os.path.join(self.tree.music, "Song.flac")
        with io.open(self.track, "w", encoding="utf-8") as handle:
            handle.write("x" * 4096)
        self.set_config(FILE_DIRECTORY=self.tree.music,
                        LOCAL_LIST_DIR=self.tree.lists,
                        LIST_BASE_NAME="DCCoreTest", NICKNAME="DCCoreTest",
                        CHANNEL="#chan", PAUSE_ON_UPDATE=True,
                        search_inprogress=False, update_inprogress=False)

    def request_file(self, user="dave"):
        self.oserve.queued.clear()
        dcc.handle_download_request(RecordingSocket(), user, "Song.flac", "#chan")
        return "".join(m for _u, m, *_ in self.oserve.queued)

    def search(self, user="dave", term="song"):
        self.oserve.queued.clear()
        list_mod.execute_search(RecordingSocket(), user, term, "#chan")
        return "".join(m for _u, m, *_ in self.oserve.queued)


class AFileRequestSurvivesAConcurrentSearch(SearchCase):

    def test_a_search_in_progress_does_not_refuse_a_file_request(self):
        """The defect itself. Someone else typing @find must not make this
        user's request fail."""
        self.set_config(search_inprogress=True)

        text = self.request_file()

        self.assertNotIn("currently rebuilding", text,
                         "a search refused an unrelated file request")

    def test_the_user_is_not_told_a_rebuild_is_happening(self):
        """The message was the worst part: it sent the operator looking at the
        list build for a problem that was contention on a read."""
        self.set_config(search_inprogress=True)

        self.assertNotIn("MasterList", self.request_file())

    def test_a_real_rebuild_still_refuses(self):
        """Control. The gate has to keep doing its actual job - during a
        rebuild the list files really are being replaced."""
        self.set_config(update_inprogress=True)

        text = self.request_file()

        self.assertIn("currently rebuilding", text)

    def test_the_pause_switch_still_turns_the_gate_off(self):
        """Control for the other half of the condition, which the change kept:
        PAUSE_ON_UPDATE False means sharing continues during a rebuild."""
        self.set_config(update_inprogress=True, PAUSE_ON_UPDATE=False)

        self.assertNotIn("currently rebuilding", self.request_file())


class ASecondSearcherIsToldWhy(SearchCase):

    def test_a_concurrent_search_gets_an_answer_at_all(self):
        """It used to print to the console and return. From the user's side
        their @find simply vanished."""
        self.set_config(search_inprogress=True)

        text = self.search()

        self.assertTrue(text, "the second searcher was sent nothing at all")

    def test_and_the_answer_is_accurate(self):
        """Not 'MasterList rebuild', which is what everything near this branch
        used to say regardless of what was actually happening."""
        self.set_config(search_inprogress=True)

        text = self.search()

        self.assertIn("Another search", text)
        self.assertNotIn("rebuild", text)

    def test_a_rebuild_still_gets_the_rebuild_message(self):
        """Control: the two branches must not collapse into one message."""
        self.set_config(update_inprogress=True)

        text = self.search()

        self.assertIn("rebuild", text)
        self.assertNotIn("Another search", text)


class TheTwoFlagsMeanDifferentThings(unittest.TestCase):
    """The point of the change, stated where a reader will find it."""

    def test_the_transfer_gate_reads_update_inprogress(self):
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as handle:
            source = handle.read()
        gate = source[source.index("The global maintenance gate"):][:1400]

        self.assertIn("update_inprogress", gate)
        self.assertNotIn("if getattr(config, 'PAUSE_ON_UPDATE', True) is True "
                         "and getattr(config, 'search_inprogress', False)", gate)

    def test_search_inprogress_still_exists_for_search_exclusion(self):
        """It was not deleted - one search at a time is still wanted."""
        with io.open(os.path.join(REPO_ROOT, "list.py"), encoding="utf-8") as handle:
            source = handle.read()

        self.assertIn("config.search_inprogress = True", source)
        self.assertIn("config.search_inprogress = False", source)


if __name__ == "__main__":
    unittest.main()
