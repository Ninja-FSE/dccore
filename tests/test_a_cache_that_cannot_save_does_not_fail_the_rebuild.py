"""An audio cache that cannot be saved does not fail a published rebuild (#980).

The audio-info cache is saved after the new list has been swapped in. A locked
or unwritable cache there (another rebuild, a database browser holding it) went
to the rebuild's own failure path: the rebuild was reported failed while its
list was live - and with several lists, "still serving what they last built",
which was untrue. Now it is said, and the rebuild succeeds.
"""

import io
import sqlite3
import unittest
from contextlib import redirect_stdout

from tests import support  # noqa: F401  (path setup)

import audio_info  # noqa: E402
import list as list_mod  # noqa: E402
import update_list  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_the_list_says_how_long_and_how_good as long_and_good  # noqa: E402


class ALockedCache(long_and_good.FileCase):
    def setUp(self):
        super().setUp()
        self.set_config(LOCAL_LIST_DIR=self.tree.lists, LIST_BASE_NAME="DCCoreTest",
                        NICKNAME="DCCoreTest", RAR_ENABLED=False, LIST_FORMAT="txt",
                        LIST_SHOW_AUDIO_INFO=True)
        self.write("Example Artist - 01 - Opening.mp3", long_and_good.frames(100), folder="Album")

        def locked(cache):
            raise sqlite3.OperationalError("database is locked")

        real = audio_info.Cache.publish
        audio_info.Cache.publish = locked
        self.addCleanup(setattr, audio_info.Cache, "publish", real)

    def test_the_rebuild_still_succeeds_and_says_why_the_cache_did_not(self):
        said = io.StringIO()
        with redirect_stdout(said):
            built = update_list.generate_master_list()
        self.assertTrue(built, said.getvalue())
        self.assertIsNotNone(list_mod.find_latest_list())
        self.assertIn("the audio info cache could not be updated (database is locked)", said.getvalue())
        self.assertNotIn("LIST-GEN ERROR", said.getvalue())


if __name__ == "__main__":
    unittest.main()
