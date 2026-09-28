"""The list archive, asked for by name, waits for the whole rebuild (#971).

Since #923 file requests are served while a rebuild scans, reads audio info
and writes - the published list is untouched until the swap. "@nick" still
waits for the whole rebuild, but "!Bot <base>-<date>.zip" asks for the same
archive by name, and a second rebuild on the same day keeps that name. A slow
receiver started mid-scan held the archive open, and on Windows the swap's
replace gave up and the whole rebuild rolled back.
"""

import io
import os
import unittest

from tests import support  # noqa: F401  (path setup)

import dcc  # noqa: E402
import defaults as config  # noqa: E402
import update_list  # noqa: E402

# Imported as a module, not by name: a TestCase class imported by name is
# collected and run again here.
import tests.test_path_security as path_security  # noqa: E402


class TheArchiveWaits(path_security.PathSecurityBase):
    def setUp(self):
        super().setUp()
        self.name = f"{config.LIST_BASE_NAME}-2026-09-01.zip"
        with io.open(os.path.join(self.tree.lists, self.name), "wb") as handle:
            handle.write(b"not a real zip, just bytes")
        self.set_config(PAUSE_ON_UPDATE=True, PAUSE_FOR_WHOLE_UPDATE=False, search_inprogress=False)

    def ask(self, name):
        self.oserve.queued.clear()
        with path_security.quiet():
            dcc.handle_download_request(self.sock, "dave", name, "#dccore-test")
        return "".join(message for _user, message, *_ in self.oserve.queued)

    def sent(self):
        return "sending" in [kind for kind, _args in self.notices]

    def test_not_during_the_scan(self):
        self.set_config(update_inprogress=True)
        update_list.write_progress("scanning", force=True)
        self.assertIn("Master list is currently rebuilding", self.ask(self.name))
        self.assertFalse(self.sent())

    def test_served_once_the_rebuild_is_done(self):
        self.set_config(update_inprogress=False)
        self.ask(self.name)
        self.assertTrue(self.sent())

    def test_other_files_are_still_served_during_the_scan(self):
        """#923 stands: only the archive waits."""
        self.set_config(update_inprogress=True)
        update_list.write_progress("scanning", force=True)
        reply = self.ask(os.path.basename(self.tree.tracks[0]))
        self.assertNotIn("rebuilding", reply)
        self.assertTrue(self.sent())


if __name__ == "__main__":
    unittest.main()
