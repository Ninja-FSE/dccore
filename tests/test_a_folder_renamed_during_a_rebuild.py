"""#1383: a folder renamed or removed while the list is rebuilt is left out of
that list, rather than costing the whole rebuild.

#441 aborted on any subtree that vanished mid-walk, reasoning that the scan
described a state that no longer existed - and kept the previous list, which
named that same folder and lacked everything added since. On a network share a
scan takes minutes, so one rename by the operator cost a day's list.

What still aborts, and keeps the previous index, is the LIBRARY going away: the
configured folder being walked disappearing or being replaced (an unmounted
share leaves an empty mount point behind) either when the error arrives or when
its walk ends - and every error that is not "this path is really gone": EIO,
a FileNotFoundError whose path is still there, Windows' "network path not
found" (53) and "network name not found" (67), permission denied on a path
that is gone.

Every case runs the real generate_master_list(). Most stub
update_list.walk_with_sizes() to call its onerror the way a real unreadable
subtree would, as UnreadableSubtreeKeepsThePreviousIndex does; one deletes a
real folder after the walk has queued it, so the error comes from os.scandir
itself.
"""

import contextlib
import io
import os
import shutil
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import library  # noqa: E402
import update_list  # noqa: E402

from tests.test_master_list_generation import MasterListCase  # noqa: E402

# The log lines this change adds. Matched as fragments rather than whole lines
# so that rewording the rest of the sentence does not break the test.
SKIPPED_LINE = "moved or removed during the scan"
SUMMARY_LINE = "changed during the scan and were left out of this list"
ROOT_WENT_LINE = "itself went away during the scan"


class VanishCase(MasterListCase):
    """A library with one listed album, then a second album added - so a
    rebuild that publishes is visibly different from one that kept the
    previous list."""

    OLD_TRACK = "Invented Band/First Record/01 - Opening.flac"
    NEW_TRACK = "Invented Band/Second Record/02 - Follow Up.flac"

    def setUp(self):
        super().setUp()
        self.add(self.OLD_TRACK)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertTrue(self.generate(), "the baseline rebuild must publish")
        self.previous_list = self.read_list()
        self.add(self.NEW_TRACK)
        # Every error the walk reported, whatever the collector made of it -
        # so a test can prove its error really reached _on_walk_error().
        self.reported = []
        # The real walk, kept once: a test that hooks the walk again (one
        # subTest per case) replaces the hook rather than wrapping it.
        self.real_walk = update_list.walk_with_sizes
        self.addCleanup(setattr, update_list, "walk_with_sizes", self.real_walk)

    def hook_walk(self, before=None, after=None, workers=None, on_dir=None):
        """Stand in for update_list.walk_with_sizes(), with the real walk
        inside it. `before(top, onerror)` runs before the walk starts,
        `after(top, onerror)` once it is exhausted, and `on_dir(top, dirpath)`
        as each directory is handed to the scan."""
        real = self.real_walk
        test = self

        def fake(top, onerror=None):
            def recording(err):
                test.reported.append(err)
                onerror(err)

            if before is not None:
                before(top, recording)
            kwargs = {} if workers is None else {"workers": workers}
            for item in real(top, onerror=recording, **kwargs):
                if on_dir is not None:
                    on_dir(top, item[0])
                yield item
            if after is not None:
                after(top, recording)

        update_list.walk_with_sizes = fake

    def report_before_walking(self, *errors):
        """Report `errors(top)` through onerror before the walk starts."""
        def before(top, onerror):
            for make in errors:
                onerror(make(top))
        self.hook_walk(before=before)

    @staticmethod
    def gone(top, name="Renamed Away"):
        """A FileNotFoundError for a folder under `top` that is not there,
        shaped like the one os.scandir raises."""
        return FileNotFoundError(2, "No such file or directory", os.path.join(top, name))

    @staticmethod
    def gone_with_winerror(winerror, name="Renamed Away"):
        """A FileNotFoundError carrying a Windows error code, on any platform.

        OSError(None, msg, path, winerror) builds one only on Windows - the
        fourth argument is ignored elsewhere - so the attribute is set on an
        ordinary FileNotFoundError instead. The collector reads it with
        getattr(), exactly as it reads the one a Windows scandir raises."""
        def make(top):
            err = VanishCase.gone(top, name)
            err.winerror = winerror
            return err
        return make

    def generate_logged(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = self.generate()
        return result, out.getvalue()

    def move_library_away(self):
        """Rename the configured folder out of the way, and put it back when
        the test ends whatever happens."""
        music = self.tree.music
        away = music + "-elsewhere"
        os.rename(music, away)

        def put_back():
            if os.path.isdir(away):
                if os.path.isdir(music):
                    shutil.rmtree(music)
                os.rename(away, music)
        self.addCleanup(put_back)
        return put_back

    def assert_published(self, result, log):
        self.assertTrue(result, f"the rebuild did not publish. Log:\n{log}")
        self.assertIn("02 - Follow Up.flac", self.read_list(),
                      "the list published is not this scan's")

    def assert_kept_previous(self, result, log):
        self.assertFalse(result, f"the rebuild published. Log:\n{log}")
        self.assertEqual(self.read_list(), self.previous_list,
                         "a rebuild that failed replaced the previous list")


class AFolderThatVanishedIsLeftOut(VanishCase):

    def test_a_vanished_subfolder_is_skipped_and_the_rest_published(self):
        self.report_before_walking(self.gone)

        result, log = self.generate_logged()

        self.assert_published(result, log)
        self.assertIn(SKIPPED_LINE, log)
        self.assertIn("Renamed Away", log)
        self.assertIn("01 - Opening.flac", self.read_list())

    def test_a_folder_really_deleted_after_the_walk_queued_it(self):
        """The real thing: no fabricated error. With one worker the walk
        queues a directory's subfolders as it hands that directory over, so
        deleting one at that moment makes os.scandir() itself fail on it -
        FileNotFoundError, WinError 3 on Windows and ENOENT elsewhere."""
        self.add("Invented Band/Doomed Record/03 - Never Listed.flac")
        state = {"deleted": False}

        def on_dir(top, dirpath):
            if not state["deleted"] and os.path.basename(dirpath) == "Invented Band":
                shutil.rmtree(os.path.join(dirpath, "Doomed Record"))
                state["deleted"] = True

        self.hook_walk(workers=1, on_dir=on_dir)

        result, log = self.generate_logged()

        self.assertTrue(state["deleted"], "the walk never reached the parent folder")
        self.assertEqual(len(self.reported), 1, f"expected one walk error: {self.reported!r}")
        self.assertIsInstance(self.reported[0], FileNotFoundError)
        self.assertIn("Doomed Record", self.reported[0].filename)
        self.assert_published(result, log)
        self.assertNotIn("03 - Never Listed.flac", self.read_list())
        self.assertIn(SKIPPED_LINE, log)

    def test_windows_errors_2_and_3_are_a_folder_that_is_gone(self):
        for winerror in (2, 3):
            with self.subTest(winerror=winerror):
                self.reported.clear()
                self.report_before_walking(self.gone_with_winerror(winerror))

                result, log = self.generate_logged()

                self.assertEqual(self.reported[0].winerror, winerror)
                self.assert_published(result, log)

    def test_a_real_windows_path_not_found_is_skipped(self):
        """Paired with the test above, which runs everywhere: here the error
        is built the way Windows builds it, from the code alone."""
        if os.name != "nt":
            self.skipTest("OSError builds a FileNotFoundError from a Windows "
                          "error code only on Windows")
        self.report_before_walking(
            lambda top: OSError(None, "The system cannot find the path specified",
                                os.path.join(top, "Renamed Away"), 3))

        result, log = self.generate_logged()

        self.assertIsInstance(self.reported[0], FileNotFoundError)
        self.assert_published(result, log)


class TheLibraryGoingAwayStillAborts(VanishCase):
    """#441's case. The configured folder going away mid-walk looks, one
    subfolder at a time, exactly like a series of renames."""

    def test_the_configured_folder_gone_when_the_error_arrives(self):
        def before(top, onerror):
            put_back = self.move_library_away()
            try:
                onerror(self.gone(top))
            finally:
                put_back()

        self.hook_walk(before=before)

        result, log = self.generate_logged()

        self.assert_kept_previous(result, log)
        self.assertNotIn(SKIPPED_LINE, log)

    def test_the_configured_folder_replaced_when_the_error_arrives(self):
        """An unmounted share leaves its mount point behind: still a
        directory, so "is it a directory" would pass. It is not the same
        directory."""
        def before(top, onerror):
            put_back = self.move_library_away()
            os.mkdir(self.tree.music)
            try:
                onerror(self.gone(top))
            finally:
                put_back()

        self.hook_walk(before=before)

        result, log = self.generate_logged()

        self.assert_kept_previous(result, log)
        self.assertNotIn(SKIPPED_LINE, log)

    def test_the_configured_folder_gone_when_its_walk_ends(self):
        """The subfolder was skipped while its parent stood; the parent then
        went too, so it was the share and not a rename."""
        self.hook_walk(before=lambda top, onerror: onerror(self.gone(top)),
                       after=lambda top, onerror: self.move_library_away())

        result, log = self.generate_logged()

        self.assert_kept_previous(result, log)
        self.assertIn(SKIPPED_LINE, log, "the error was not skipped at first")
        self.assertIn(ROOT_WENT_LINE, log)
        self.assertNotIn(SUMMARY_LINE, log)

    def test_the_configured_folder_replaced_when_its_walk_ends(self):
        def after(top, onerror):
            self.move_library_away()
            os.mkdir(self.tree.music)

        self.hook_walk(before=lambda top, onerror: onerror(self.gone(top)), after=after)

        result, log = self.generate_logged()

        self.assert_kept_previous(result, log)
        self.assertIn(ROOT_WENT_LINE, log)

    def test_the_folder_moving_after_a_clean_walk_does_not_matter(self):
        """Control: the end-of-walk question is asked only when something
        under the folder vanished."""
        self.hook_walk(after=lambda top, onerror: self.move_library_away())

        result, log = self.generate_logged()

        self.assertTrue(result, log)
        self.assertNotIn(ROOT_WENT_LINE, log)


class EveryOtherWalkErrorIsStillFatal(VanishCase):

    def assert_fatal(self, make):
        self.report_before_walking(make)

        result, log = self.generate_logged()

        self.assertEqual(len(self.reported), 1)
        self.assert_kept_previous(result, log)
        self.assertNotIn(SKIPPED_LINE, log)

    def test_not_found_for_a_path_that_is_still_there(self):
        self.assert_fatal(lambda top: FileNotFoundError(
            2, "No such file or directory", os.path.join(top, "Invented Band")))

    def test_network_path_and_share_not_found(self):
        """WinError 53 and 67 are FileNotFoundError too, but about the share."""
        for winerror in (53, 67):
            with self.subTest(winerror=winerror):
                self.reported.clear()
                self.assert_fatal(self.gone_with_winerror(winerror))

    def test_a_real_windows_network_path_not_found(self):
        if os.name != "nt":
            self.skipTest("OSError builds a FileNotFoundError from a Windows "
                          "error code only on Windows")
        self.assert_fatal(lambda top: OSError(
            None, "The network path was not found", os.path.join(top, "Renamed Away"), 53))
        self.assertIsInstance(self.reported[0], FileNotFoundError)

    def test_an_input_output_error(self):
        self.assert_fatal(lambda top: OSError(5, "Input/output error",
                                              os.path.join(top, "Renamed Away")))

    def test_not_found_with_no_path(self):
        self.assert_fatal(lambda top: FileNotFoundError(2, "No such file or directory"))

    def test_permission_denied_on_a_path_that_is_gone(self):
        self.assert_fatal(lambda top: PermissionError(
            13, "Permission denied", os.path.join(top, "Renamed Away")))


class TheOperatorIsToldWhatWasLeftOut(VanishCase):

    def vanish(self, count):
        self.report_before_walking(*[
            (lambda top, n=n: self.gone(top, f"Moved Folder {n}"))
            for n in range(1, count + 1)])
        result, log = self.generate_logged()
        self.assert_published(result, log)
        lines = [line for line in log.splitlines() if SUMMARY_LINE in line]
        self.assertLessEqual(len(lines), 1, lines)
        return lines[0] if lines else None

    def test_no_summary_when_nothing_vanished(self):
        result, log = self.generate_logged()
        self.assertTrue(result, log)
        self.assertNotIn(SUMMARY_LINE, log)
        self.assertNotIn(SKIPPED_LINE, log)

    def test_five_are_all_named(self):
        summary = self.vanish(5)
        self.assertIsNotNone(summary)
        self.assertIn("5 folder(s)", summary)
        for n in range(1, 6):
            self.assertIn(f"Moved Folder {n}'", summary)
        self.assertFalse(summary.endswith(" ..."), summary)

    def test_more_than_five_names_the_first_five(self):
        summary = self.vanish(6)
        self.assertIsNotNone(summary)
        self.assertIn("6 folder(s)", summary)
        for n in range(1, 6):
            self.assertIn(f"Moved Folder {n}'", summary)
        self.assertNotIn("Moved Folder 6", summary)
        self.assertTrue(summary.endswith(" ..."), summary)



class EachConfiguredFolderIsJudgedOnItsOwn(VanishCase):
    """Two configured folders. The folder an error is judged against, and the
    errors its end-of-walk check takes back, are the one being walked now -
    not the first, and not every folder walked so far."""

    def setUp(self):
        super().setUp()
        self.second = os.path.join(self.tree.root, "second-library")
        track = os.path.join(self.second, "Other Band", "Other Record", "01 - Elsewhere.flac")
        os.makedirs(os.path.dirname(track))
        with open(track, "wb") as handle:
            handle.write(b"\x00" * 4096)
        self.set_config(LIBRARY_FOLDERS_FILE=os.path.join(self.tree.root, "library_folders.json"))
        library.save_folders([library.Folder("Music", self.tree.music),
                              library.Folder("Second", self.second)])

    def is_second(self, top):
        return os.path.basename(top) == "second-library"

    def move_second_away(self):
        away = self.second + "-elsewhere"
        os.rename(self.second, away)

        def put_back():
            if os.path.isdir(away):
                if os.path.isdir(self.second):
                    shutil.rmtree(self.second)
                os.rename(away, self.second)
        self.addCleanup(put_back)
        return put_back

    def test_the_second_folder_gone_when_its_error_arrives(self):
        """Judged against the FIRST folder, which is still there, this would
        be skipped - and the second folder's share dropping would publish a
        list without it."""
        def before(top, onerror):
            if self.is_second(top):
                put_back = self.move_second_away()
                try:
                    onerror(self.gone(top))
                finally:
                    put_back()

        self.hook_walk(before=before)

        result, log = self.generate_logged()

        self.assertEqual(len(self.reported), 1)
        self.assert_kept_previous(result, log)
        self.assertNotIn(SKIPPED_LINE, log)

    def test_a_skip_in_the_first_folder_is_not_the_second_folders_business(self):
        """The second folder walks cleanly and then moves. Its end-of-walk
        check must look only at what vanished under it - nothing - so the
        first folder's skip does not turn into an abort."""
        def before(top, onerror):
            if not self.is_second(top):
                onerror(self.gone(top))

        def after(top, onerror):
            if self.is_second(top):
                self.move_second_away()

        self.hook_walk(before=before, after=after)

        result, log = self.generate_logged()

        self.assert_published(result, log)
        self.assertIn("01 - Elsewhere.flac", self.read_list())
        self.assertIn(SKIPPED_LINE, log)
        self.assertNotIn(ROOT_WENT_LINE, log)


class TheFolderIsAskedAboutBeforeThePath(VanishCase):
    """A share that drops and comes back between the two questions.

    _on_walk_error() asks two things of a not-found error: is the configured
    folder still the same directory (os.stat of the root), and is the path
    really gone (os.path.lexists). Here the share is down for whichever of
    those is asked FIRST and back up for the second. Root first: the root is
    gone, so it aborts. Path first: the path reads as gone, then the root as
    unchanged - and a subtree that is in fact still there is skipped, which is
    the truncated list #441 exists to refuse.
    """

    def test_a_share_flapping_between_the_two_checks_aborts(self):
        asked = []

        def before(top, onerror):
            err = self.gone(top)
            root = os.path.normcase(top)
            gone_name = os.path.basename(os.path.normcase(err.filename))
            real_stat, real_lexists = os.stat, os.path.lexists

            def share_down():
                return len(asked) == 1

            def stat(target, *args, **kwargs):
                if os.path.normcase(os.fspath(target)) == root:
                    asked.append("root")
                    if share_down():
                        raise FileNotFoundError(2, "No such file or directory", target)
                return real_stat(target, *args, **kwargs)

            def lexists(target):
                if os.path.basename(os.path.normcase(os.fspath(target))) == gone_name:
                    asked.append("path")
                    # Back up, the path is there; down, it is not.
                    return not share_down()
                return real_lexists(target)

            # Only around this one call: nothing else in the scan sees them.
            os.stat, os.path.lexists = stat, lexists
            try:
                onerror(err)
            finally:
                os.stat, os.path.lexists = real_stat, real_lexists

        self.hook_walk(before=before)

        result, log = self.generate_logged()

        self.assertTrue(asked, "neither check reached the stand-ins")
        self.assert_kept_previous(result, log)
        self.assertNotIn(SKIPPED_LINE, log)
        self.assertEqual(asked[0], "root", f"asked in this order: {asked}")


if __name__ == "__main__":
    unittest.main()
