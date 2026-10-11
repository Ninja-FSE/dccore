"""A request gets the file it names, and only what it names.

Three ways a request reached more than that, each through the real request
and send paths:

- On Windows, a name with a ':' in it opened an NTFS alternate data stream of
  a library file. A stream sits inside the library as far as the containment
  check can tell, and the one Windows writes on every downloaded file records
  where that file was downloaded from. A name ending in a dot or a space
  opened the file under a second spelling.
- A list archive is asked for by its file name, and a non-primary list's
  directory sits inside the primary's. A name with that list's folder in
  front of it reached the other list's archive from a channel it is not
  bound to.
- A DCC RESUME at (or past) the end of a file completes the send with nothing
  on the wire. It was counted as a download all the same, credited to the
  file and announced in the channel.

The stream tests run on every platform. On Windows the stream is a real one;
elsewhere an ordinary file whose name holds the ':' stands in for it, and the
bot is told it runs on Windows - which is the only thing that makes a ':'
mean "stream", so it is the only thing switched on. The resume tests run
against fake sockets: nothing is bound or dialled.
"""

import io
import os
import struct
import sys
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import db  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import library  # noqa: E402
import list as list_mod  # noqa: E402
import platform_compat  # noqa: E402
import stats_mgr  # noqa: E402

from tests.test_dcc_resume_end_to_end import RecordingIrcSocket  # noqa: E402
from tests.test_every_transfer_ending_is_recorded_with_how_it_ended import (  # noqa: E402
    CONTENT, FakeListener, FakeReceiver, SendCase, USER, rows)
from tests.test_path_security import InlineThread, PathSecurityBase, quiet  # noqa: E402

HIDDEN = dcc.names_a_hidden_part_of_a_file
STREAM = dcc.resolves_to_a_stream
STREAM_TEXT = "[ZoneTransfer]\nZoneId=3\nHostUrl=https://downloads.example/get?key=alfa\n"


class TheNameGuard(unittest.TestCase):
    """names_a_hidden_part_of_a_file() on its own: what it refuses, where."""

    def test_a_stream_is_refused_on_windows(self):
        for name in ("Artist/Album/track.flac:Zone.Identifier",
                     "track.flac:Zone.Identifier",
                     "track.flac::$DATA",
                     "Artist\\Album\\track.flac:hidden:$DATA",
                     "Album:secret/track.flac"):
            self.assertTrue(HIDDEN(name, windows=True), name)

    def test_a_trimmed_second_spelling_is_refused_on_windows(self):
        for name in ("track.flac.", "track.flac ", "track.flac. .",
                     "Artist./Album/track.flac", "Artist/Album /track.flac"):
            self.assertTrue(HIDDEN(name, windows=True), name)

    def test_the_same_characters_are_ordinary_names_elsewhere(self):
        """':' and a trailing dot are legal on Linux and macOS, and files
        named with them are real and must stay requestable there."""
        for name in ("Live: 1999.flac", "Alfa: The Album/01 - Intro.flac",
                     "A.B.C./Album/track.flac", "track.flac.", "Album /track.flac"):
            self.assertFalse(HIDDEN(name, windows=False), name)

    def test_a_resource_fork_is_refused_everywhere(self):
        for windows in (True, False):
            self.assertTrue(HIDDEN("Album/track.flac/..namedfork/rsrc", windows=windows))
            self.assertTrue(HIDDEN("track.flac/..NamedFork/rsrc", windows=windows))

    def test_ordinary_names_pass_on_both(self):
        for name in ("Song.flac", "Artist/Album (2024)/01 - Song.flac",
                     "Artist - Album [FLAC]/Track 1.flac",
                     "Alfa Brav\u00f3 - \u00c1lbum \u00e6 eins.flac",
                     "Vol. 1/01 - .hidden start.flac", "Album/../Album/Song.flac",
                     "./Song.flac", "Con.Alfa.Soundtrack/01.flac"):
            for windows in (True, False):
                self.assertFalse(HIDDEN(name, windows=windows), (name, windows))

    def test_the_default_follows_the_platform(self):
        with mock.patch.object(platform_compat, "IS_WINDOWS", True):
            self.assertTrue(HIDDEN("track.flac:Zone.Identifier"))
        with mock.patch.object(platform_compat, "IS_WINDOWS", False):
            self.assertFalse(HIDDEN("track.flac:Zone.Identifier"))


class TheResolvedPathGuard(unittest.TestCase):
    """resolves_to_a_stream(): the second gate, on the path the lookup chose."""

    def test_a_colon_after_the_drive_is_a_stream(self):
        self.assertTrue(STREAM("C:\\Music\\Album\\track.flac:Zone.Identifier", windows=True))
        self.assertTrue(STREAM("\\\\server\\share\\Album\\track.flac::$DATA", windows=True))

    def test_the_drive_itself_is_not(self):
        self.assertFalse(STREAM("C:\\Music\\Album\\track.flac", windows=True))
        self.assertFalse(STREAM("\\\\server\\share\\Album\\track.flac", windows=True))

    def test_nothing_is_a_stream_off_windows(self):
        self.assertFalse(STREAM("/srv/music/Live: 1999.flac", windows=False))


class AStreamIsNotServed(PathSecurityBase):
    """Through handle_download_request(), with a stream that really exists."""

    def setUp(self):
        super().setUp()
        album = os.path.join(self.tree.music, "Artist", "Album")
        os.makedirs(album, exist_ok=True)
        self.track = os.path.join(album, "track.flac")
        with io.open(self.track, "wb") as handle:
            handle.write(b"fLaC" * 100)
        self.stream = self.track + ":Zone.Identifier"
        try:
            with io.open(self.stream, "w") as handle:
                handle.write(STREAM_TEXT)
        except OSError as err:
            self.skipTest(f"this volume cannot hold {self.stream!r}: {err}")
        self.assertTrue(os.path.exists(self.stream))
        patches = [mock.patch.object(platform_compat, "IS_WINDOWS", True)]
        if os.name != "nt":
            # long_path() would prefix a POSIX path with \\?\ once told it is
            # on Windows; on Windows it is unchanged, so it stays as it is.
            patches.append(mock.patch.object(platform_compat, "long_path", lambda path: path))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def request(self, name):
        with quiet():
            dcc.handle_download_request(self.sock, "alfa", name, "#dccore-test")

    def sent_paths(self):
        return [os.path.normcase(str(args[2])) for name, args in InlineThread.dispatched
                if name == "start_dcc_send"]

    def assertRefused(self, name):
        self.assertEqual(self.sent_paths(), [], f"{name!r} was sent")
        self.assertEqual(config.dcc_queue, {}, f"{name!r} was queued")
        self.assertIn(("error", ("alfa", "invalid_path", "#dccore-test")), self.notices)

    def test_a_stream_of_a_library_file_is_refused(self):
        self.request("Artist/Album/track.flac:Zone.Identifier")
        self.assertRefused("Artist/Album/track.flac:Zone.Identifier")

    def test_the_main_stream_by_its_other_name_is_refused(self):
        if os.name != "nt":
            with io.open(self.track + "::$DATA", "wb") as handle:
                handle.write(b"fLaC")
        self.request("Artist/Album/track.flac::$DATA")
        self.assertRefused("Artist/Album/track.flac::$DATA")

    def test_the_request_is_refused_on_its_text(self):
        """The first gate, alone: refused before the lookup settles on a path."""
        with mock.patch.object(dcc, "resolves_to_a_stream", lambda *a, **k: False):
            self.request("Artist/Album/track.flac:Zone.Identifier")
        self.assertRefused("Artist/Album/track.flac:Zone.Identifier")

    def test_a_trimmed_second_spelling_is_refused(self):
        """Windows opens "track.flac." as "track.flac"; elsewhere a file by
        that literal name stands in for what Windows would open."""
        if os.name != "nt":
            with io.open(self.track + ".", "wb") as handle:
                handle.write(b"fLaC")
        self.assertTrue(os.path.exists(self.track + "."))
        self.request("Artist/Album/track.flac.")
        self.assertRefused("Artist/Album/track.flac.")

    def test_the_resolved_path_is_checked_too(self):
        """The second gate, alone: whatever produced the path, a stream of a
        file is refused before it is offered."""
        with mock.patch.object(dcc, "names_a_hidden_part_of_a_file", lambda *a, **k: False):
            self.request("Artist/Album/track.flac:Zone.Identifier")
        self.assertRefused("Artist/Album/track.flac:Zone.Identifier")

    def test_the_file_itself_is_still_sent(self):
        """The control: the guard must not cost the file its own request."""
        self.request("Artist/Album/track.flac")
        self.assertEqual(self.sent_paths(), [os.path.normcase(self.track)])

    def test_a_queued_stream_is_not_sent(self):
        """A row that names a stream - restored from dcc_queue.txt, never
        through the request guards - is refused by the dispatch-time check
        every queued row passes before its send."""
        row = {"file": "track.flac:Zone.Identifier", "path": self.stream}
        self.assertFalse(dcc.may_still_be_sent(self.stream, row))

    def test_a_queued_file_is_still_sent(self):
        """The control for the one above."""
        row = {"file": "track.flac", "path": self.track}
        self.assertTrue(dcc.may_still_be_sent(self.track, row))


class AListArchiveStaysWithItsChannel(PathSecurityBase):
    """Two lists: Alfa, the primary, bound to #music; Bravo bound to #vip."""

    DATE = "2026-10-01"

    def setUp(self):
        super().setUp()
        alfa = os.path.join(self.tree.root, "alfa-folder")
        bravo = os.path.join(self.tree.root, "bravo-folder")
        for path in (alfa, bravo):
            os.makedirs(path, exist_ok=True)
        self.set_config(LIST_BASE_NAME="SomeBot", NICKNAME="SomeBot", CHANNEL="#music,#vip")
        library.save_lists([
            library.ServedList(name="Alfa", primary=True, channels=("#music",),
                               folders=(library.Folder("Alfa", alfa),)),
            library.ServedList(name="Bravo", primary=False, channels=("#vip",),
                               folders=(library.Folder("Bravo", bravo),)),
        ])
        self.archive_name = f"SomeBot-{self.DATE}.zip"
        self.alfa_archive = self.write_archive("Alfa", b"PK-alfa")
        self.bravo_archive = self.write_archive("Bravo", b"PK-bravo")
        self.assertNotEqual(os.path.dirname(self.alfa_archive),
                            os.path.dirname(self.bravo_archive))

    def write_archive(self, list_name, content):
        directory = list_mod.list_dir(list_name)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, self.archive_name)
        with io.open(path, "wb") as handle:
            handle.write(content)
        return path

    def request(self, name, channel):
        with quiet():
            dcc.handle_download_request(self.sock, "alfa", name, channel)

    def sent_paths(self):
        return [os.path.normcase(os.path.abspath(str(args[2])))
                for name, args in InlineThread.dispatched if name == "start_dcc_send"]

    def test_another_lists_archive_is_not_sent_by_its_folder(self):
        bravo_folder = os.path.basename(os.path.dirname(self.bravo_archive))
        self.request(f"{bravo_folder}/{self.archive_name}", "#music")
        self.assertNotIn(os.path.normcase(os.path.abspath(self.bravo_archive)), self.sent_paths())
        self.assertEqual(self.sent_paths(), [])
        self.assertIn(("error", ("alfa", "invalid_path", "#music")), self.notices)

    def test_each_channel_still_gets_its_own_archive(self):
        """The control, for both lists."""
        self.request(self.archive_name, "#music")
        self.request(self.archive_name, "#vip")
        # The second is queued behind the first for the same nick; what each
        # one resolved to is what matters.
        resolved = self.sent_paths() + [
            os.path.normcase(os.path.abspath(row["path"]))
            for row in config.dcc_queue.get("alfa", [])]
        self.assertEqual(resolved, [os.path.normcase(os.path.abspath(self.alfa_archive)),
                                    os.path.normcase(os.path.abspath(self.bravo_archive))])


class ResumedReceiver(FakeReceiver):
    """A receiver that already held `held` bytes: like any DCC receiver it
    acknowledges ABSOLUTE positions, what it held plus what has arrived."""

    def __init__(self, held):
        super().__init__("complete")
        self.held = held

    def sendall(self, data):
        self.blocks += 1
        self.sent += len(data)
        self.pending += struct.pack("!I", self.held + self.sent)


class AResumeAtTheEndIsNotADownload(SendCase):
    """The receiver resumes at or past the end: it already holds the file."""

    def setUp(self):
        super().setUp()
        self.set_config(MSG_DELAY=0.0)
        self.counted = []
        for module, name in ((db, "update_stats_on_complete"), (db, "record_download"),
                             (stats_mgr, "update_speed_record")):
            real = getattr(module, name)
            patch = mock.patch.object(module, name, self.recorder(name, real))
            patch.start()
            self.addCleanup(patch.stop)
        self.announced = []
        patch = mock.patch.object(announce, "send_transfer_complete",
                                  lambda *a, **k: self.announced.append(a))
        patch.start()
        self.addCleanup(patch.stop)

    def recorder(self, name, real):
        def record(*args, **kwargs):
            self.counted.append(name)
            if name == "update_stats_on_complete":
                return (1, args[0])
            return None
        return record

    def resumed_send(self, position):
        irc = RecordingIrcSocket()
        receiver = ResumedReceiver(min(position, len(CONTENT)))
        FakeListener.receiver = receiver

        def accept(listener):
            # Between the offer and the connection, as a real receiver does.
            offered = dcc.offered_name_from_handshake(irc.handshake)
            self.assertTrue(dcc.handle_resume_request(
                irc, USER, f"\x01DCC RESUME {offered} {irc.port()} {position}\x01"))
            return receiver, ("192.0.2.10", 40000)

        with mock.patch.object(FakeListener, "accept", accept):
            transfer_rows = self.send(irc=irc)
        return irc, receiver, transfer_rows

    def test_a_resume_at_the_end_counts_and_announces_nothing(self):
        irc, receiver, transfer_rows = self.resumed_send(len(CONTENT))
        self.assertEqual(len(irc.accept_positions()), 1, "the receiver was not answered")
        self.assertEqual(receiver.sent, 0)
        self.assertEqual(self.counted, [])
        self.assertEqual(self.announced, [])
        self.assertEqual(transfer_rows, [])

    def test_a_resume_past_the_end_counts_and_announces_nothing(self):
        _irc, receiver, transfer_rows = self.resumed_send(999_999_999)
        self.assertEqual(receiver.sent, 0)
        self.assertEqual(self.counted, [])
        self.assertEqual(self.announced, [])
        self.assertEqual(transfer_rows, [])

    def test_a_resume_that_sends_the_tail_still_counts(self):
        """The control: a resume that moved bytes is a download, counted for
        what it moved."""
        _irc, receiver, transfer_rows = self.resumed_send(len(CONTENT) - 1000)
        self.assertEqual(receiver.sent, 1000)
        self.assertEqual(self.counted, ["update_stats_on_complete", "record_download",
                                        "update_speed_record"])
        self.assertEqual(len(self.announced), 1)
        self.assertEqual([row[5] for row in transfer_rows], [1000])


if __name__ == "__main__":
    unittest.main()
