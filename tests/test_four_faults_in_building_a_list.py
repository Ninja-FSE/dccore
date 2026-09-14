"""Four faults in the thing that takes three hours to run.

A rebuild over an 80TB library is the most expensive operation the daemon
performs, and the least often observed - so a fault here is paid for in hours
and noticed late:

  * #441 one permanently unreadable folder - a Windows volume root's System
    Volume Information, a POSIX lost+found - aborted every rebuild, for ever,
    on that install;
  * #442 a failure in the tail work AFTER the swap reported that the previous
    list was kept, when the new one was already live and serving;
  * #443 the film and series list was never sorted, so it came out in whatever
    order the filesystem handed the directories over;
  * #444 the !update re-entrancy guard read its flag 178 lines before setting
    it, with a wait in between.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import runtime  # noqa: E402


def source(name):
    with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def without_comments(text):
    text = re.sub(chr(35) + "[^" + chr(10) + "]*", "", text)
    return re.sub(r'"""..*?"""', "", text, flags=re.S)


class APermanentlyDeniedFolderIsNotFatal(unittest.TestCase):
    """#441. The distinction is whether the path is still there: permission
    denied on a directory that exists is a fact about the ACL and will be true
    on every future scan; a directory that vanished mid-scan means the library
    changed underneath us and the snapshot is already wrong."""

    @staticmethod
    def classify(err):
        """The decision the collector makes, lifted verbatim."""
        denied = isinstance(err, PermissionError)
        if denied:
            try:
                denied = os.path.isdir(err.filename)
            except Exception:
                denied = False
        return "excluded" if denied else "aborts"

    def error(self, kind, path, errno=13):
        """errno matters: OSError(13, ...) CONSTRUCTS a PermissionError -
        Python picks the subclass from the errno - so a test wanting a plain
        OSError has to ask for one that is not a permission error."""
        err = kind(errno, "denied")
        err.filename = path
        return err

    def test_denied_but_still_there_is_excluded(self):
        import tempfile

        self.assertEqual(
            self.classify(self.error(PermissionError, tempfile.mkdtemp())),
            "excluded")

    def test_denied_and_gone_still_aborts(self):
        """The library changed under the scan. Publishing that snapshot would
        be publishing a state known not to exist."""
        self.assertEqual(
            self.classify(self.error(PermissionError, os.path.join(
                REPO_ROOT, "no-such-directory-anywhere"))),
            "aborts")

    def test_any_other_error_still_aborts(self):
        import tempfile

        # errno 5 (EIO), not 13: OSError(13, ...) is a PermissionError.
        err = self.error(OSError, tempfile.mkdtemp(), errno=5)
        self.assertNotIsInstance(err, PermissionError)

        self.assertEqual(self.classify(err), "aborts")

    def test_the_collector_really_makes_that_distinction(self):
        code = without_comments(source("update_list.py"))
        collector = code.split("def _on_walk_error", 1)[1].split("ignored =", 1)[0]

        self.assertIn("PermissionError", collector)
        self.assertIn("os.path.isdir", collector,
                      "nothing checks whether the folder is still there, so a "
                      "vanished one would be excluded like a denied one")

    def test_denied_folders_do_not_reach_the_abort(self):
        code = without_comments(source("update_list.py"))

        self.assertIn("denied_dirs", code)
        abort = code.split("if walk_errors:", 1)[1].split("return False", 1)[0]
        self.assertNotIn("denied_dirs", abort,
                         "a denied folder counts toward the abort again")

    def test_the_operator_is_told_the_list_is_short_and_why(self):
        """A standing exclusion nobody is told about looks like a bug in the
        scanner when the file count comes up short."""
        code = source("update_list.py")

        self.assertIn("will not be in the next one until their permissions", code)


class AFailureAfterTheSwapSaysSo(unittest.TestCase):
    """#442. Everything from the swap to the handler is tail work that can
    raise, and the handler claimed the old list was kept."""

    def test_published_is_declared_before_the_try_that_reads_it(self):
        """Declared inside, an early failure meets an unbound name in the very
        handler meant to report it - turning a reported failure into a
        different, unreported one."""
        code = source("update_list.py")
        declared = code.index("published = False")
        try_at = code.index("try:", declared)
        set_at = code.index("published = True")
        read_at = code.index("if published:")

        self.assertLess(declared, try_at)
        self.assertLess(try_at, set_at)
        self.assertLess(set_at, read_at)

    def test_it_is_set_only_after_the_swap(self):
        code = source("update_list.py")

        self.assertLess(code.index("_publish_artifacts(swaps)"),
                        code.index("published = True"))

    def test_the_handler_stops_claiming_the_old_list_was_kept(self):
        code = source("update_list.py")
        handler = code.split("Failed to generate the lists", 1)[1][:900]

        self.assertIn("if published:", handler)
        self.assertIn("WAS published", handler,
                      "the handler does not tell the operator the new list is "
                      "already live")


class TheVideoListIsSorted(unittest.TestCase):
    """#443."""

    def test_it_is_sorted_the_same_way_the_master_list_is(self):
        code = without_comments(source("update_list.py"))

        self.assertIn("video_files_data.sort(", code)
        master = re.search(r"all_files_data\.sort\(key=([^\n]+)\)", code)
        video = re.search(r"video_files_data\.sort\(key=([^\n]+)\)", code)
        self.assertTrue(master and video)
        self.assertEqual(master.group(1), video.group(1),
                         "the two lists are ordered by different keys, so the "
                         "same folder sorts to a different place in each")


class TheUpdateGuardChecksAndSetsTogether(unittest.TestCase):
    """#444."""

    def test_the_gate_lives_where_a_rehash_cannot_rebind_it(self):
        """commands.py is reloaded by !rehash; a lock constructed there is a
        fresh object on the far side of every reload (#235)."""
        self.assertTrue(hasattr(runtime, "list_update_gate"))
        self.assertNotIn("list_update_gate = threading.Lock()",
                         source("commands.py"))

    def test_the_flag_is_set_inside_the_same_block_that_checks_it(self):
        code = without_comments(source("commands.py"))
        gate = code.split("with runtime.list_update_gate:", 1)[1][:600]

        self.assertIn("update_inprogress", gate)
        self.assertIn("config.update_inprogress = True", gate,
                      "the flag is still set somewhere below the gate, which "
                      "is the window two requests both passed through")

    def test_the_denial_path_puts_the_flag_back(self):
        """Raised by the gate and then returned past would deny every future
        update for the life of the process."""
        code = without_comments(source("commands.py"))
        denial = code.split("Another system scan is already running", 1)[1][:400]

        self.assertIn("config.update_inprogress = False", denial)
