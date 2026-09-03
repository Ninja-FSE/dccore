"""Regression tests for dcc.py path containment.

These guard the directory-traversal fix: without it any channel user could type
"!rar ../../root/.ssh" and have that folder packed and DCC'd to them, and the
first version of the containment check used a bare ``startswith`` that also let
a sibling directory sharing the music root's string prefix through
("/srv/library-backup" vs "/srv/library").

Everything here is stdlib-only and runs against real temp directories, because
the behaviour under test is about how the filesystem resolves paths.
"""

import contextlib
import io
import os
import threading
import unittest

from tests.support import (DCCoreTestCase, no_disk_writes, silence_debug,
                           RecordingSocket)

import announce
import defaults as config
import db
import dcc


@contextlib.contextmanager
def quiet():
    """Swallow the daemon's console output.

    dcc.py prints Swedish text on the security paths; on a console whose code
    page cannot encode it (cp1253, and emoji on cp1252) the print itself would
    raise and be eaten by the caller's try/except, hiding what is really being
    tested. Redirecting to a StringIO keeps the tests deterministic everywhere.
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        yield buffer


class InlineThread:
    """threading.Thread stand-in that records dispatches instead of spawning.

    dcc.py hands every follow-up step to a daemon thread, so intercepting thread
    creation is the only way to keep these tests single-threaded and
    deterministic. Only the rar packer is executed in-line - that closure is the
    unit under test in PoisonedQueueRowTests and dcc.py starts it after dropping
    queue_lock. check_queue_and_send is started from INSIDE that lock and takes
    it again itself, so running it here would deadlock; start_dcc_send would
    open a real socket. Both are recorded only.
    """

    RUN_INLINE = ("inline_rar_packer",)
    dispatched = []

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_ignored):
        self.target = target
        self.args = args
        self.kwargs = kwargs or {}
        self.daemon = daemon

    def start(self):
        name = getattr(self.target, "__name__", "")
        InlineThread.dispatched.append((name, self.args))
        if self.target is not None and name in InlineThread.RUN_INLINE:
            self.target(*self.args, **self.kwargs)

    def join(self, timeout=None):
        return None


class PathSecurityBase(DCCoreTestCase):
    """Shared plumbing: a temp music tree, stubbed announce/db, no real threads."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.sock = RecordingSocket()

        no_disk_writes(db)
        self.debug = silence_debug(announce)

        # announce's notice helpers would format mIRC colour codes onto a live
        # socket; the tests only care that they were reached.
        self.notices = []
        self._saved_announce = {}
        for name, recorder in (
            ("send_pack_error_notice", lambda *a, **k: self.notices.append(("pack_error", a))),
            ("send_dcc_queue_notice", lambda *a, **k: self.notices.append(("queue", a))),
            ("send_dcc_sending_notice", lambda *a, **k: self.notices.append(("sending", a))),
            ("send_dcc_error", lambda *a, **k: self.notices.append(("error", a))),
        ):
            self._saved_announce[name] = getattr(announce, name, None)
            setattr(announce, name, recorder)

        # dcc.threading is the real threading module, so this patches thread
        # creation for every code path the call touches, including the local
        # "import threading" inside check_queue_and_send.
        self._real_thread = threading.Thread
        InlineThread.dispatched = []
        dcc.threading.Thread = InlineThread

        self.addCleanup(self._restore)

    def _restore(self):
        dcc.threading.Thread = self._real_thread
        for name, original in self._saved_announce.items():
            if original is not None:
                setattr(announce, name, original)

    def debug_categories(self):
        return [category for category, _text in self.debug]

    def dispatched_names(self):
        return [name for name, _args in InlineThread.dispatched]


class IsSafePathTests(DCCoreTestCase):
    """dcc.is_safe_path - the containment primitive itself."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()

    def test_path_inside_the_music_root_is_accepted(self):
        """Defect guard: containment must not reject legitimate album paths."""
        self.assertTrue(dcc.is_safe_path(self.tree.music, self.tree.album))
        self.assertTrue(dcc.is_safe_path(self.tree.music, self.tree.tracks[0]))

    def test_the_root_itself_is_accepted(self):
        """Defect guard: base == path is inside the jail, not outside it."""
        self.assertTrue(dcc.is_safe_path(self.tree.music, self.tree.music))
        # A trailing separator must not change the verdict.
        self.assertTrue(dcc.is_safe_path(self.tree.music, self.tree.music + os.sep))

    def test_path_outside_the_music_root_is_rejected(self):
        """Defect guard: the traversal target (a secrets dir next door) must fail."""
        self.assertFalse(dcc.is_safe_path(self.tree.music, self.tree.secret))
        self.assertFalse(
            dcc.is_safe_path(self.tree.music, os.path.join(self.tree.secret, "id_rsa")))

    def test_sibling_sharing_the_root_prefix_is_rejected(self):
        """Defect guard: the bare-startswith bug.

        tree.sibling is music + "-backup": a DIFFERENT directory whose absolute
        path happens to begin with the music root's string. The original
        implementation compared with matchpath.startswith(base) and accepted it,
        exposing every backup mount sitting next to the library. The fix compares
        per directory step (base + os.sep).
        """
        self.assertTrue(self.tree.sibling.startswith(self.tree.music),
                        "fixture invariant: sibling must share the root's string prefix")
        self.assertFalse(dcc.is_safe_path(self.tree.music, self.tree.sibling))
        self.assertFalse(
            dcc.is_safe_path(self.tree.music, os.path.join(self.tree.sibling, "id_rsa")))

    def test_traversal_inside_a_valid_path_is_resolved_before_comparison(self):
        """Defect guard: ".." must be normalised away, not compared literally."""
        escaping = os.path.join(self.tree.music, "Metallica", "..", "..", "secret")
        self.assertFalse(dcc.is_safe_path(self.tree.music, escaping))
        staying = os.path.join(self.tree.music, "Metallica", "..", "Metallica")
        self.assertTrue(dcc.is_safe_path(self.tree.music, staying))

    def test_a_blank_base_dir_is_rejected_not_raised(self):
        """#184's review: FILE_DIRECTORY is deliberately not in
        settings_file.REQUIRED any more, so every caller here can now be
        handed None. os.path.realpath(None) is a TypeError - this must
        refuse instead, since every caller is a security boundary where
        "nothing to check against" has to read as "not safe", not as an
        unhandled exception."""
        self.assertFalse(dcc.is_safe_path(None, self.tree.tracks[0]))
        self.assertFalse(dcc.is_safe_path("", self.tree.tracks[0]))


class TempZipCacheFileTests(DCCoreTestCase):
    """dcc._is_temp_zip_cache_file - decides whether start_dcc_send()'s
    finally block is allowed to delete a file once nothing needs it anymore.

    Defect guard: this used to be a literal "tmp_zips" substring check
    against the shipped DEFAULT directory name, not config.TMP_ZIP_DIR's
    actual configured value. Every test fixture happens to use that default
    name, which is exactly why nothing had caught it - renaming the setting
    (a documented, supported override) made this condition never true again,
    silently, and packed .rar archives leaked on disk forever."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()

    def test_a_file_in_the_configured_temp_dir_is_eligible(self):
        path = os.path.join(config.TMP_ZIP_DIR, "Some_Album.rar")
        self.assertTrue(dcc._is_temp_zip_cache_file(path))

    def test_still_eligible_after_the_directory_is_renamed(self):
        """The regression this guards against."""
        renamed = os.path.join(self.tree.root, "packing-cache")
        os.makedirs(renamed, exist_ok=True)
        config.TMP_ZIP_DIR = renamed

        path = os.path.join(renamed, "Some_Album.rar")
        self.assertTrue(dcc._is_temp_zip_cache_file(path))

    def test_a_file_outside_the_temp_dir_is_not_eligible(self):
        """A library file must never be treated as disposable cache."""
        self.assertFalse(dcc._is_temp_zip_cache_file(self.tree.tracks[0]))

    def test_the_master_list_zip_is_excluded_even_inside_the_temp_dir(self):
        zip_path = os.path.join(config.TMP_ZIP_DIR, f"{config.LIST_BASE_NAME}.zip")
        self.assertFalse(dcc._is_temp_zip_cache_file(zip_path))


class RarRequestTraversalTests(PathSecurityBase):
    """handle_download_request's "!rar" branch - the first line of defence."""

    def request(self, spec, user="dave"):
        with quiet():
            dcc.handle_download_request(self.sock, user, "!rar " + spec, "#dccore-test")

    def assertNothingQueued(self, spec):
        self.assertEqual(
            config.dcc_queue, {},
            "!rar %r was queued; the traversal guard did not hold" % (spec,))
        self.assertEqual(
            self.dispatched_names(), [],
            "!rar %r dispatched work despite being refused" % (spec,))

    def test_parent_traversal_to_a_real_directory_is_refused(self):
        """Defect guard: "!rar ../secret" packed and sent an out-of-jail directory.

        The traversal target exists on disk, so nothing but the containment
        check can stop it - the later os.path.isdir test would happily pass.
        """
        self.assertTrue(os.path.isdir(self.tree.secret))
        self.request("../secret")
        self.assertNothingQueued("../secret")
        self.assertIn("HARDBAN", self.debug_categories())
        self.assertIn("pack_error", [kind for kind, _a in self.notices])

    def test_bare_parent_traversal_is_refused(self):
        """Defect guard: "!rar ../.." resolved to the parent of the whole library."""
        self.request("../..")
        self.assertNothingQueued("../..")
        self.assertIn("HARDBAN", self.debug_categories())

    def test_traversal_to_prefix_sibling_is_refused(self):
        """Defect guard: bare startswith let "<music>-backup" through the request path.

        End-to-end version of the is_safe_path prefix bug: the request resolves
        to a real, existing directory beside the library whose absolute path
        shares the music root's string prefix.
        """
        sibling_leaf = os.path.basename(self.tree.sibling)
        self.request("../" + sibling_leaf)
        self.assertNothingQueued("../" + sibling_leaf)
        self.assertIn("HARDBAN", self.debug_categories())

    def test_backslash_separated_traversal_is_refused(self):
        """Defect guard: mIRC sends Windows-style paths, folded to "/" before the join."""
        self.request("..\\secret")
        self.assertNothingQueued("..\\secret")
        self.assertIn("HARDBAN", self.debug_categories())

    def test_mid_path_traversal_is_refused(self):
        """Defect guard: a valid artist prefix must not launder a traversal.

        "Metallica/../../secret" contains a slash, so the old artist-root check
        waved it through; only normalising the finished path catches it.
        """
        self.request("Metallica/../../secret")
        self.assertNothingQueued("Metallica/../../secret")
        self.assertIn("HARDBAN", self.debug_categories())

    def test_absolute_path_is_refused(self):
        """Defect guard: an absolute path must never escape the music root.

        Two independent mechanisms cover this: the leading separator is stripped
        before the join (POSIX form), and on Windows a drive-qualified path
        survives the join and is then caught by is_safe_path. Either way nothing
        may be queued.
        """
        absolute = os.path.abspath(self.tree.secret)
        self.request(absolute)
        self.assertNothingQueued(absolute)

    def test_legitimate_album_request_is_accepted(self):
        """Defect guard: the traversal fix must not break ordinary folder packing."""
        self.request("Metallica/Black Album (1991)")

        self.assertIn("dave", config.dcc_queue)
        rows = config.dcc_queue["dave"]
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(os.path.normpath(row["path"]), os.path.normpath(self.tree.album))
        self.assertTrue(dcc.is_safe_path(config.FILE_DIRECTORY, row["path"]))
        # Parentheses are kept so AutoQ.mrc can still match the archive name.
        self.assertEqual(row["file"], "Black_Album_(1991).rar")
        self.assertTrue(row["is_unpacked_rar_folder"])
        self.assertEqual(row["channel"], "#dccore-test")
        self.assertEqual(row["user_raw"], "dave")
        # And the queue worker really was woken for it.
        self.assertIn("check_queue_and_send", self.dispatched_names())

    def test_rar_disabled_refuses_the_request_before_touching_the_path(self):
        """RAR_ENABLED = False refuses every !rar request outright, before any
        traversal check, path resolution, or queueing runs - a legitimate
        album request must be turned away exactly like a hostile one."""
        self.set_config(RAR_ENABLED=False)

        self.request("Metallica/Black Album (1991)")

        self.assertNothingQueued("Metallica/Black Album (1991)")
        self.assertIn(("error", ("dave", "rar_disabled")), self.notices,
                      "refused, but not for the reason this test is pinning")

    def test_rar_enabled_by_default(self):
        """Defect guard: an operator who never sets RAR_ENABLED must keep
        !rar working exactly as before this setting existed."""
        self.assertTrue(config.RAR_ENABLED)

        self.request("Metallica/Black Album (1991)")

        self.assertIn("dave", config.dcc_queue)

    def test_bracket_tags_are_kept_so_autoq_can_match_the_archive_name(self):
        """Defect guard: AutoQ.mrc's own completion check de-underscores the
        received filename and compares it to the basename of the folder path
        it queued - it never touches brackets. Stripping "[WEB]"/"[192K]"-style
        tags here left the two sides of that comparison permanently unequal,
        so the transfer completed correctly but the request never left
        AutoQ's queue window."""
        tagged = os.path.join(self.tree.music, "Metallica", "Master Of Puppets [WEB] [320K]")
        os.makedirs(tagged, exist_ok=True)
        with io.open(os.path.join(tagged, "track.flac"), "w", encoding="utf-8") as handle:
            handle.write("AUDIO")

        # The trailing slash matters: a real "!rar" request always carries one
        # (mIRC's own folder path never ends bare on the leaf name), and
        # without it the request's own trailing-bracket-tag strip - meant for
        # an appended annotation, not the folder's own name - would eat the
        # "[320K]" this test exists to prove survives.
        self.request("Metallica/Master Of Puppets [WEB] [320K]/")

        rows = config.dcc_queue["dave"]
        self.assertEqual(rows[0]["file"], "Master_Of_Puppets_[WEB]_[320K].rar")

    def test_artist_root_request_is_still_refused(self):
        """Defect guard: containment must not shadow the artist-root rule.

        A single-segment path is inside the jail, so it passes is_safe_path; the
        separate root-folder rule is what stops someone packing a whole artist.
        """
        self.request("Metallica")
        self.assertNothingQueued("Metallica")


class DownloadRequestChannelValidationTests(PathSecurityBase):
    """#162 finding #5's second half: target_chan becomes this request's
    queue row "channel" - what announce.py later replays into an outbound
    "Sent: ..." advert line on completion - so it is re-validated at the
    one place it gets persisted, even though irc.py's own PRIVMSG parser
    is now anchored so target_chan can only ever be the literal wire
    target of the message (see irc.parse_privmsg()). Must be a channel
    this bot is actually configured to be in, or its own nick (a private
    request)."""

    def request(self, target_chan, user="dave"):
        with quiet():
            dcc.handle_download_request(
                self.sock, user, "!rar Metallica/Black Album (1991)", target_chan)

    def test_the_configured_channel_is_accepted(self):
        self.assertIn("#dccore-test", config.CHANNEL.split(","))
        self.request("#dccore-test")
        self.assertIn("dave", config.dcc_queue)

    def test_a_second_configured_channel_is_accepted(self):
        self.set_config(CHANNEL="#dccore-test,#dccore-test3")
        self.request("#dccore-test3")
        self.assertIn("dave", config.dcc_queue)

    def test_the_bots_own_nick_is_accepted_a_private_request(self):
        self.request(config.NICKNAME)
        self.assertIn("dave", config.dcc_queue)

    def test_channel_matching_is_case_insensitive(self):
        self.request("#DCCORE-TEST")
        self.assertIn("dave", config.dcc_queue)

    def test_a_channel_this_bot_is_not_in_is_refused(self):
        self.request("#not-our-channel")
        self.assertEqual(config.dcc_queue, {},
                         "a target_chan outside config.CHANNEL must never "
                         "reach the queue")

    def test_an_arbitrary_nick_is_refused(self):
        """The exact shape #5's own repro described: an attacker-forged
        target_chan naming another user, not a channel or this bot."""
        self.request("SomeVictim")
        self.assertEqual(config.dcc_queue, {})


class PoisonedQueueRowTests(PathSecurityBase):
    """inline_rar_packer's second line of defence, reached via check_queue_and_send.

    Queue rows are persisted to dcc_queue.txt and survive a restart, so a row
    written before the traversal guard existed would otherwise still be packed.
    """

    def setUp(self):
        super().setUp()
        config.channel_users = {"#dccore-test": {"dave"}}
        config.bot_joined_channel = True

        # Stub the packer engine: reaching it at all is the failure this class
        # is about, and a real "rar" binary must never be required by the suite.
        self.rar_calls = []
        self._real_run = dcc.subprocess.run

        class FakeCompleted:
            returncode = 1
            stdout = ""
            stderr = "stubbed rar"

        def fake_run(cmd, *a, **kw):
            self.rar_calls.append(cmd)
            return FakeCompleted()

        dcc.subprocess.run = fake_run
        self.addCleanup(lambda: setattr(dcc.subprocess, "run", self._real_run))

        # dcc.py now resolves the rar binary through platform_compat BEFORE calling
        # subprocess.run, and raises if it cannot find one. CI runners have no rar
        # installed, so without stubbing the lookup this test would assert on a call
        # that never happens - and would pass or fail purely on whether the machine
        # running it happens to have WinRAR. Pin it so the test measures the code,
        # not the host.
        import platform_compat
        self._real_rar_command = platform_compat.rar_command
        platform_compat.rar_command = lambda configured=None: os.path.join(
            self.tree.root, "rar.exe" if os.name == "nt" else "rar")
        self.addCleanup(
            lambda: setattr(platform_compat, "rar_command", self._real_rar_command))

    def make_row(self, path):
        """A persisted !rar row, exactly as handle_download_request writes it."""
        return {
            "file": "Loot.rar",
            "path": path,
            "channel": "#dccore-test",
            "user_raw": "dave",
            "is_unpacked_rar_folder": True,
            "is_temporary_zip": True,
        }

    def test_poisoned_row_outside_the_root_is_discarded_not_packed(self):
        """Defect guard: a persisted row pointing outside the music root got packed.

        The packer must drop the row, never invoke rar on it, and leave the
        process-wide rar_inprogress interlock clear so other users can still pack.
        """
        row = self.make_row(self.tree.secret)
        config.dcc_queue["dave"] = [row]

        with quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertEqual(self.rar_calls, [],
                         "rar was invoked on a path outside the music root")
        self.assertEqual(config.dcc_queue.get("dave", []), [],
                         "the poisoned row survived in the queue")
        self.assertFalse(getattr(config, "rar_inprogress", False),
                         "rar_inprogress stayed latched, wedging packing for everyone")
        self.assertNotIn("dave", config.user_processing_lock)
        self.assertIn("HARDBAN", self.debug_categories())
        self.assertNotIn("start_dcc_send", self.dispatched_names())

    def test_poisoned_row_on_a_prefix_sibling_is_discarded(self):
        """Defect guard: the prefix-sibling hole, through the persisted-queue path."""
        row = self.make_row(self.tree.sibling)
        config.dcc_queue["dave"] = [row]

        with quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertEqual(self.rar_calls, [])
        self.assertEqual(config.dcc_queue.get("dave", []), [])
        self.assertFalse(getattr(config, "rar_inprogress", False))
        self.assertNotIn("start_dcc_send", self.dispatched_names())

    def test_legitimate_row_still_reaches_the_packer(self):
        """Control: proves the discard tests above are not passing vacuously.

        Identical setup with an in-jail path must get all the way to the rar
        invocation, so the two tests above really do measure the containment
        check and not some earlier bail-out.
        """
        row = self.make_row(self.tree.album)
        config.dcc_queue["dave"] = [row]

        with quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertEqual(len(self.rar_calls), 1)
        # The command comes from platform_compat.rar_command rather than the literal
        # "rar" - WinRAR installs rar.exe outside PATH, so a bare name never runs on
        # Windows. Assert it is the resolved binary, not a fixed string.
        invoked = os.path.basename(self.rar_calls[0][0]).lower()
        self.assertIn(invoked, ("rar", "rar.exe"), self.rar_calls[0][0])
        self.assertIn(os.path.abspath(self.tree.album), self.rar_calls[0])
        # The stubbed rar failed, so the row is kept for retry rather than dropped.
        self.assertEqual(config.dcc_queue.get("dave", []), [row])
        self.assertEqual(row.get("send_fails"), 1)
        self.assertFalse(getattr(config, "rar_inprogress", False))


class FileDirectoryUnsetRequestTests(PathSecurityBase):
    """#184's review: FILE_DIRECTORY is deliberately not in
    settings_file.REQUIRED any more (see its own comment) - the daemon can be
    up, joined and answering requests before an operator has chosen a music
    directory. Before this fix, a file or !rar request in that state reached
    os.path.abspath(None)/is_safe_path(None, ...), both TypeErrors, caught by
    handle_download_request's own bare `except Exception` and printed to the
    console with no NOTICE of any kind sent back - the requester got no
    response at all, not even "not found"."""

    def setUp(self):
        super().setUp()
        self.set_config(FILE_DIRECTORY=None)

    def test_a_plain_file_request_gets_an_actionable_notice_not_silence(self):
        with quiet():
            dcc.handle_download_request(self.sock, "dave", "Song.flac", "#dccore-test")

        self.assertIn(("error", ("dave", "not_configured")), self.notices)
        self.assertEqual(config.dcc_queue, {})
        self.assertEqual(self.dispatched_names(), [])

    def test_a_rar_request_gets_an_actionable_notice_not_silence(self):
        with quiet():
            dcc.handle_download_request(
                self.sock, "dave", "!rar Metallica/Black Album (1991)", "#dccore-test")

        self.assertIn(("error", ("dave", "not_configured")), self.notices)
        self.assertEqual(config.dcc_queue, {})
        self.assertEqual(self.dispatched_names(), [])

    def test_a_list_artifact_request_is_unaffected(self):
        """Control: a request for the master list itself never touches
        FILE_DIRECTORY (it resolves against LOCAL_LIST_DIR instead), so it
        must keep working exactly as if FILE_DIRECTORY were set."""
        name = f"{config.LIST_BASE_NAME}-2026-09-01.zip"
        path = os.path.join(self.tree.lists, name)
        with io.open(path, "wb") as handle:
            handle.write(b"not a real zip, just bytes")

        with quiet():
            dcc.handle_download_request(self.sock, "dave", name, "#dccore-test")

        self.assertNotIn(("error", ("dave", "not_configured")), self.notices)
        self.assertIn("sending", [kind for kind, _a in self.notices])


if __name__ == "__main__":
    unittest.main()
