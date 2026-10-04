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

import types

import dcc  # noqa: E402
import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import runtime  # noqa: E402
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


class TheArchiveWaitsForTheReadingsSwap(TheArchiveWaits):
    """The background audio reading (#1182) packs the archive again and swaps
    it in once, after the rebuild is over. The archive waits through that -
    "packing" and "publishing", the window searches and requests already
    pause for - or a Windows send holding it open makes the re-publish give
    up. The same check and the same message as a rebuild's."""

    def setUp(self):
        super().setUp()
        import commands
        self.set_config(update_inprogress=False)
        commands.start_audio_watch(types.SimpleNamespace(pid=1), "rebuild", start=lambda: None)

    def test_refused_while_it_packs_and_while_it_swaps(self):
        """PAUSE_ON_UPDATE off, so it is this check that answers, with a
        rebuild's own message."""
        self.set_config(PAUSE_ON_UPDATE=False)
        for phase in ("packing", "publishing"):
            with self.subTest(phase=phase):
                update_list.write_progress(phase, force=True)
                self.assertIn("Master list is currently rebuilding", self.ask(self.name))
                self.assertFalse(self.sent())

    def test_with_the_pause_on_it_is_refused_all_the_same(self):
        update_list.write_progress("publishing", force=True)
        self.assertIn("rebuilding", self.ask(self.name))
        self.assertFalse(self.sent())

    def test_served_while_it_reads(self):
        self.set_config(PAUSE_ON_UPDATE=False)
        update_list.write_progress("reading", force=True)
        self.ask(self.name)
        self.assertTrue(self.sent())

    def test_served_once_the_rebuild_is_done(self):
        """The rebuild's own test, for the reading: served while it reads."""
        update_list.write_progress("reading", force=True)
        self.ask(self.name)
        self.assertTrue(self.sent())

    def test_refused_from_its_start_and_through_its_rewrite(self):
        """Every phase but the reading itself (#1182 audit): a send started
        while the rows are rewritten would still be running at the swap."""
        self.set_config(PAUSE_ON_UPDATE=False)
        for phase in (None, "finding", "rewriting"):
            with self.subTest(phase=phase):
                if phase is None:
                    update_list.clear_progress()
                else:
                    update_list.write_progress(phase, force=True)
                self.assertIn("Master list is currently rebuilding", self.ask(self.name))
                self.assertFalse(self.sent())

    def test_served_again_once_it_is_over(self):
        """Its last phase still in the file, but the reading is over."""
        self.set_config(PAUSE_ON_UPDATE=False)
        update_list.write_progress("publishing", force=True)
        runtime.audio_reading = None
        self.ask(self.name)
        self.assertTrue(self.sent())

    def test_at_nick_waits_the_same_way(self):
        """list.send_file_list(), for "@nick": the same check."""
        queued = []
        real_queue = list_mod.oserve.queue_message
        list_mod.oserve.queue_message = lambda user, message, *rest, **kw: queued.append(message)
        self.addCleanup(setattr, list_mod.oserve, "queue_message", real_queue)
        real_find = list_mod.find_latest_list_file
        list_mod.find_latest_list_file = lambda name=None: None
        self.addCleanup(setattr, list_mod, "find_latest_list_file", real_find)
        for phase, refused in (("reading", False), ("packing", True), ("publishing", True)):
            with self.subTest(phase=phase):
                queued.clear()
                update_list.write_progress(phase, force=True)
                list_mod.send_file_list(self.sock, "dave", "#dccore-test")
                said = "".join(queued)
                self.assertEqual("Master list is currently rebuilding" in said, refused, said)
        runtime.audio_reading = None
        queued.clear()
        list_mod.send_file_list(self.sock, "dave", "#dccore-test")
        self.assertNotIn("rebuilding", "".join(queued))


if __name__ == "__main__":
    unittest.main()
