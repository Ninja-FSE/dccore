"""A rebuild says what it is doing while it does it.

Asked for during the RC1 beta: "running update list in tools is too silent."

It was. The page said "Rebuilding the master list…" and nothing else for as
long as the scan took - which on a 719k-file library is minutes, and is
indistinguishable from a hung process.

A FILE, NOT SHARED MEMORY. update_list.py runs as a SUBPROCESS - see
commands.handle_list_update_request - so it has nowhere in the daemon's memory
to report into. It writes LIST_PROGRESS_FILE and the status endpoint reads it.
The alternative was parsing the child's stdout, which turns prose written for
an operator into a wire format.

WHAT IS HONEST TO REPORT

The folder COUNT is known before the walk starts, so "folder 2 of 5" is a real
fraction. A file total is not known without a full pass - which is the work
being measured - so the bar advances per folder and a live file counter shows
movement in between. Once the walk is done the folder count has nothing left
to say, and the writing phase reports itself as indeterminate rather than
freezing a bar at its last value, which would read as a stall during the phase
that is genuinely slowest.

The percentage is folders COMPLETED, not the one in hand: a bar that jumps to
100% as the last folder starts is claiming to have finished while it is still
walking.
"""

import io
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import library  # noqa: E402
import update_list  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class TheScanReportsWhereItIs(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(LOCAL_LIST_DIR=self.tree.lists, NICKNAME="ProgBot",
                        LIST_BASE_NAME="ProgBot")
        update_list._progress_last_write[0] = 0.0

    def library_of(self, *names, tracks=8):
        folders = []
        for name in names:
            root = os.path.join(self.tree.root, name)
            os.makedirs(os.path.join(root, "Album"), exist_ok=True)
            for i in range(tracks):
                with io.open(os.path.join(root, "Album", f"{i:02d} {name}.flac"),
                             "wb") as handle:
                    handle.write(b"x" * 256)
            folders.append(library.Folder(name, root))
        library.save_lists([library.ServedList(name="Main", primary=True,
                                               channels=(), folders=tuple(folders))])

    def test_a_running_scan_is_visible_through_the_status_endpoint(self):
        """The end an operator sees: what the page polls."""
        self.library_of("Rock", "Jazz", "Metal")
        seen = []
        real_walk = update_list.walk_with_sizes

        # Hooked on walk_with_sizes(), the seam the scan uses. It hooked
        # os.walk() while the scan asked for every file's size a second time
        # through os.path.getsize(); the size now comes back with the name, so
        # os.walk() is no longer on the path and a hook there observes
        # nothing - which is a whole scan reporting no progress, and reads as
        # the progress file being broken.
        def watching_walk(top, onerror=None):
            found = webserver.read_list_progress()
            if found:
                seen.append(found)
            yield from real_walk(top, onerror=onerror)

        update_list.walk_with_sizes = watching_walk
        self.addCleanup(setattr, update_list, "walk_with_sizes", real_walk)

        update_list.generate_master_list()

        self.assertTrue(seen, "a whole scan reported nothing at all")
        self.assertTrue(any(p["folder"] for p in seen),
                        "no folder name was ever reported")
        self.assertTrue(any(p["folder_count"] == 3 for p in seen),
                        "the folder total was never reported")

    def test_the_percentage_counts_folders_completed(self):
        """Not the one in hand. A bar that reaches 100% as the last folder
        STARTS says it has finished while it is still walking."""
        update_list.write_progress("scanning", folder="Metal", folder_index=3,
                                   folder_count=3, files=120, force=True)

        progress = webserver.read_list_progress()

        self.assertEqual(progress["percent"], 66)

    def test_the_first_folder_is_zero_not_one_third(self):
        update_list.write_progress("scanning", folder="Rock", folder_index=1,
                                   folder_count=3, force=True)

        self.assertEqual(webserver.read_list_progress()["percent"], 0)

    def test_the_writing_phase_has_no_percentage(self):
        """The folder count has nothing left to say once the walk is done."""
        update_list.write_progress("writing", files=180, force=True)

        progress = webserver.read_list_progress()

        self.assertEqual(progress["phase"], "writing")
        self.assertIsNone(progress["percent"])
        self.assertEqual(progress["files"], 180)

    def test_the_file_count_climbs(self):
        """What tells an operator it is alive while the bar sits on one
        folder."""
        update_list.write_progress("scanning", files=10, force=True)
        first = webserver.read_list_progress()["files"]
        update_list.write_progress("scanning", files=99, force=True)
        second = webserver.read_list_progress()["files"]

        self.assertEqual((first, second), (10, 99))

    def test_the_file_is_gone_when_the_run_ends(self):
        """A leftover from a killed process reads as a rebuild still going,
        and the page would show a bar that never moves."""
        update_list.write_progress("scanning", files=5, force=True)
        self.assertIsNotNone(webserver.read_list_progress())

        update_list.clear_progress()

        self.assertIsNone(webserver.read_list_progress())

    def test_clearing_a_file_that_is_not_there_is_not_an_error(self):
        update_list.clear_progress()
        update_list.clear_progress()

    def test_a_completed_build_leaves_nothing_behind(self):
        """Through the real entry point, not by calling clear_progress()
        directly - the clearing lives in generate_all_lists()'s finally so
        that every caller gets it, and so that this test can reach it. An
        earlier version only cleared inside `__main__`, which no test runs,
        so a mutation removing it passed."""
        self.library_of("Rock")

        self.assertTrue(update_list.generate_all_lists(log=lambda *a, **k: None))

        self.assertIsNone(webserver.read_list_progress())

    def test_a_build_that_raises_still_leaves_nothing_behind(self):
        """A crash mid-scan must not leave a bar that never moves."""
        self.library_of("Rock")
        update_list.write_progress("scanning", files=3, force=True)
        real = update_list.generate_master_list
        update_list.generate_master_list = lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("scan exploded"))
        self.addCleanup(setattr, update_list, "generate_master_list", real)

        with self.assertRaises(RuntimeError):
            update_list.generate_all_lists(log=lambda *a, **k: None)

        self.assertIsNone(webserver.read_list_progress())


class ProgressNeverCostsTheRebuild(DCCoreTestCase):
    """This is a progress bar. A full disk or a read-only data/ must cost the
    operator the bar, not the list rebuild they actually asked for."""

    def test_an_unwritable_path_does_not_raise(self):
        self.set_config(LIST_PROGRESS_FILE=os.path.join(
            "\x00 definitely not a path", "p.json"))

        update_list.write_progress("scanning", files=1, force=True)

    def test_a_scan_still_completes_when_progress_cannot_be_written(self):
        tree = self.make_tree()
        root = os.path.join(tree.root, "Rock")
        os.makedirs(os.path.join(root, "Album"), exist_ok=True)
        with io.open(os.path.join(root, "Album", "01 a.flac"), "wb") as handle:
            handle.write(b"x" * 256)
        library.save_lists([library.ServedList(
            name="Main", primary=True, channels=(),
            folders=(library.Folder("Rock", root),))])
        self.set_config(LOCAL_LIST_DIR=tree.lists, NICKNAME="ProgBot",
                        LIST_BASE_NAME="ProgBot",
                        LIST_PROGRESS_FILE=os.path.join("\x00 nope", "p.json"))

        self.assertTrue(update_list.generate_master_list())


class TheStatusPayloadStaysUsable(DCCoreTestCase):

    def test_no_progress_file_means_no_progress_key(self):
        """The page falls back to its old wording, rather than rendering an
        empty bar."""
        update_list.clear_progress()

        self.assertNotIn("progress", webserver.build_update_list_status_payload())

    def test_a_malformed_file_is_ignored_rather_than_raising(self):
        """Half a JSON object is what a read landing mid-write would get if
        the writer did not rename into place. The reader must survive it
        either way - this endpoint is polled every couple of seconds."""
        with io.open(config.LIST_PROGRESS_FILE, "w", encoding="utf-8") as handle:
            handle.write('{"phase": "scan')

        self.assertIsNone(webserver.read_list_progress())

    def test_a_json_value_that_is_not_an_object_is_ignored(self):
        with io.open(config.LIST_PROGRESS_FILE, "w", encoding="utf-8") as handle:
            json.dump([1, 2, 3], handle)

        self.assertIsNone(webserver.read_list_progress())

    def test_nonsense_numbers_do_not_reach_the_page(self):
        """The file is written by a subprocess; the page divides by
        folder_count."""
        with io.open(config.LIST_PROGRESS_FILE, "w", encoding="utf-8") as handle:
            json.dump({"phase": "scanning", "folder": "x", "folder_index": "many",
                       "folder_count": -4, "files": None}, handle)

        progress = webserver.read_list_progress()

        self.assertEqual(progress["folder_index"], 0)
        self.assertEqual(progress["folder_count"], 0)
        self.assertEqual(progress["files"], 0)
        self.assertIsNone(progress["percent"])

    def test_an_index_beyond_the_count_is_clamped(self):
        """Otherwise the bar renders past 100%."""
        with io.open(config.LIST_PROGRESS_FILE, "w", encoding="utf-8") as handle:
            json.dump({"phase": "scanning", "folder_index": 99,
                       "folder_count": 3}, handle)

        progress = webserver.read_list_progress()

        self.assertEqual(progress["folder_index"], 3)
        self.assertLessEqual(progress["percent"], 100)

    def test_a_long_folder_name_is_truncated(self):
        """It is rendered into the page, and a peer's folder name has no
        length anybody promised."""
        with io.open(config.LIST_PROGRESS_FILE, "w", encoding="utf-8") as handle:
            json.dump({"phase": "scanning", "folder": "z" * 5000}, handle)

        self.assertLessEqual(len(webserver.read_list_progress()["folder"]), 120)


class ThePageRendersIt(unittest.TestCase):

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                     encoding="utf-8") as handle:
            return handle.read()

    def test_the_poll_shows_progress_rather_than_one_fixed_line(self):
        source = self.source()

        self.assertIn("showUpdateListProgress(payload.progress)", source)

    def progress_body(self):
        """showUpdateListProgress()'s body, to the start of the next function.

        This used to be a fixed character budget - [:400] and [:1600] - which
        is a bet on the function never growing. It grew: adding the elapsed
        clock pushed "is-indeterminate" past 1600 characters and failed a test
        that was not about the clock at all, while the code it checks was
        untouched and still correct.
        """
        marker = chr(10) + "  function "
        return self.source().split(
            "function showUpdateListProgress(", 1)[1].split(marker, 1)[0]

    def test_the_extraction_stops_at_the_next_function(self):
        """Fixture invariant. If the split stopped matching, the body would be
        the whole rest of the file and every check below would pass on
        something else's code."""
        body = self.progress_body()

        self.assertIn("el.updateListBarFill", body)
        self.assertNotIn("function pollUpdateListStatus", body)

    def test_it_falls_back_when_there_is_no_progress_yet(self):
        """The first poll can land before the child has written anything."""
        self.assertIn("Rebuilding the master list", self.progress_body())

    def test_the_writing_phase_is_indeterminate(self):
        self.assertIn("is-indeterminate", self.progress_body())

    def test_the_bar_markup_exists(self):
        with io.open(os.path.join(REPO_ROOT, "web", "index.html"),
                     encoding="utf-8") as handle:
            markup = handle.read()

        self.assertIn('id="update-list-bar"', markup)
        self.assertIn('id="update-list-bar-fill"', markup)

    def test_reduced_motion_is_respected(self):
        """An animation that cannot be stopped is a problem for anybody who
        has asked the system not to move things."""
        with io.open(os.path.join(REPO_ROOT, "web", "style.css"),
                     encoding="utf-8") as handle:
            css = handle.read()

        self.assertIn("prefers-reduced-motion", css)
        reduced = css.split("prefers-reduced-motion", 1)[1][:300]
        self.assertIn("animation: none", reduced)


if __name__ == "__main__":
    unittest.main()
