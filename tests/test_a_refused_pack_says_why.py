"""A refused !rar request is told why it was refused (#1475).

announce.send_pack_error_notice() had one fixed text - "Artist root folders
cannot be requested ..." - and every refusal in dcc.handle_download_request()
sent it. A folder over MAX_RAR_FOLDER_SIZE, or one holding nothing in
RAR_EXTENSIONS, was told it was an artist root; only the debug channel had the
real reason.

Each refusal is driven here through the real request handler, with the real
notice builder, and what reaches oserve.queue_message() is read back. The
frame and colours are the golden ones (tests/_golden_palette.py); only the
Error text differs per reason.
"""

import contextlib
import io
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import dcc  # noqa: E402
import defaults as config  # noqa: E402
import update_list  # noqa: E402

from tests._golden_palette import GOLDEN  # noqa: E402
from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_path_security import PathSecurityBase, quiet  # noqa: E402

OLD_TEXT = ("Artist root folders cannot be requested. "
            "Please select a specific album sub-folder.")
NOT_OFFERED = "That folder is not one this bot offers."
NOTHING_PACKABLE = ("That folder holds nothing this bot packs. "
                    "Its files can still be requested by name.")

# The golden notice, for user "dave", as the classic theme sends it.
GOLDEN_LINE = GOLDEN["send_pack_error_notice"][0]


def framed(text):
    """The golden notice with only its Error text replaced."""
    error = "Error: " + OLD_TEXT + " "
    assert GOLDEN_LINE.count(error) == 1, GOLDEN_LINE
    return GOLDEN_LINE.replace(error, "Error: " + text + " ")


def too_large(limit_text):
    return ("That folder is larger than the %s limit for packed folders. "
            "Its files can still be requested one by one." % limit_text)


# Placeholder host, the same form the existing remote-path tests use.
REMOTE = "\\\\host.example\\share\\Album"


class RefusalCase(PathSecurityBase):
    """The real request handler and the REAL send_pack_error_notice: only
    the other notice helpers and thread creation are recorded."""

    def setUp(self):
        super().setUp()
        # PathSecurityBase records send_pack_error_notice; put the real one
        # back, so what is asserted is what the requester would receive.
        announce.send_pack_error_notice = self._saved_announce["send_pack_error_notice"]
        self.set_config(THEME="classic", RAR_ENABLED=True)

    def request(self, spec, user="dave", channel="#dccore-test"):
        self.oserve.queued.clear()
        with quiet() as printed:
            dcc.handle_download_request(self.sock, user, "!rar " + spec, channel)
        self.printed = printed.getvalue()
        return list(self.oserve.queued)

    def assertRefusedWith(self, spec, text):
        sent = self.request(spec)
        self.assertEqual(sent, [("dave", framed(text), True)],
                         "!rar %r was not told %r" % (spec, text))
        self.assertEqual(config.dcc_queue, {}, "!rar %r was queued" % (spec,))
        self.assertEqual(self.sock.sent, [], "the notice bypassed oserve's queue")
        return sent[0][1]


class EachRefusalSaysItsOwnReason(RefusalCase):

    def test_a_remote_path_is_not_one_this_bot_offers(self):
        self.assertRefusedWith(REMOTE, NOT_OFFERED)
        self.assertIn("names a remote location", self.printed,
                      "refused, but not by the remote-path check")

    def test_a_path_outside_the_library_is_not_one_this_bot_offers(self):
        self.assertTrue(os.path.isdir(self.tree.secret))
        self.assertRefusedWith("../secret", NOT_OFFERED)
        self.assertIn("HARDBAN", self.debug_categories(),
                      "refused, but not by the containment check")

    def test_an_artist_root_keeps_the_old_text(self):
        self.assertRefusedWith("Metallica", OLD_TEXT)
        self.assertIn("pack the root folder", self.printed,
                      "refused, but not by the artist-root check")

    def test_a_folder_with_nothing_packable_says_so(self):
        notes = os.path.join(self.tree.music, "Metallica", "Liner Notes")
        os.makedirs(notes)
        with open(os.path.join(notes, "notes.txt"), "wb") as handle:
            handle.write(b"text")

        self.assertRefusedWith("Metallica/Liner Notes/", NOTHING_PACKABLE)
        self.assertIn("holds nothing in RAR_EXTENSIONS", self.printed)

    def test_a_folder_over_the_cap_names_the_cap(self):
        # The album holds 2 x 4096 bytes; a cap of 5000 shows as 4.9KB, and
        # the measured size could only show as something else.
        cap = 5000
        self.set_config(MAX_RAR_FOLDER_SIZE=cap)
        over, measured = update_list.pack_size_over(self.tree.album, cap)
        self.assertTrue(over)
        limit_text = announce.format_size_human(cap)
        self.assertEqual(limit_text, "4.9KB")
        self.assertNotEqual(announce.format_size_human(measured), limit_text)

        line = self.assertRefusedWith("Metallica/Black Album (1991)/", too_large(limit_text))

        self.assertIn("over 5,000 bytes", self.printed,
                      "refused, but not by the size check")
        self.assertNotIn(announce.format_size_human(measured), line)
        self.assertNotIn(str(measured), line)
        self.assertNotIn("8.0KB", line)
        self.assertNotIn("Black Album", line)
        self.assertNotIn("Metallica", line)
        for path in (self.tree.root, self.tree.music, self.tree.album):
            self.assertNotIn(path, line)
        self.assertNotIn("/", line)
        self.assertNotIn("\\", line)

    def test_the_five_refusals_are_not_one_shared_text(self):
        """The bug in one assertion: a single text for every reason."""
        notes = os.path.join(self.tree.music, "Metallica", "Liner Notes")
        os.makedirs(notes)
        with open(os.path.join(notes, "notes.txt"), "wb") as handle:
            handle.write(b"text")
        texts = {}
        for name, spec in (("remote", REMOTE), ("outside", "../secret"),
                           ("root", "Metallica"), ("nothing", "Metallica/Liner Notes/")):
            texts[name] = self.request(spec)[0][1]
        self.set_config(MAX_RAR_FOLDER_SIZE=5000)
        texts["large"] = self.request("Metallica/Black Album (1991)/")[0][1]

        self.assertEqual(texts["remote"], texts["outside"])
        self.assertEqual(len({texts["remote"], texts["root"], texts["nothing"],
                              texts["large"]}), 4, texts)

    def test_an_album_under_the_cap_is_queued_and_the_log_is_english(self):
        """Control, and the console line that was in Swedish."""
        self.set_config(MAX_RAR_FOLDER_SIZE=10 * 1024 * 1024)

        sent = self.request("Metallica/Black Album (1991)/")

        self.assertEqual(len(config.dcc_queue.get("dave", [])), 1)
        self.assertNotIn("DCC-PACK: Access Denied", "".join(m for _u, m, _v in sent))
        self.assertIn("[RAR QUEUE] Queued folder pack Black_Album_(1991).rar "
                      "for dave at position #1.", self.printed)
        self.assertNotIn("virtuell", self.printed)
        self.assertNotIn("mapp ", self.printed)


class TheNoticeIsStillGatedByTheChannelMode(RefusalCase):
    """notices_allowed() still decides: a Request-only channel is told nothing."""

    def setUp(self):
        super().setUp()
        store = os.path.join(self.tree.root, "lists.json")
        with io.open(store, "w", encoding="utf-8") as handle:
            json.dump([{"name": "Main", "primary": True, "channels": ["#n", "#r"],
                        "folders": [{"name": "Music", "path": self.tree.music}],
                        "modes": {"#r": "request_only"}}], handle)
        self.set_config(LISTS_FILE=store, CHANNEL="#n,#r",
                        LIBRARY_FOLDERS_FILE=os.path.join(self.tree.root, "library_folders.json"),
                        MAX_RAR_FOLDER_SIZE=5000)
        notes = os.path.join(self.tree.music, "Metallica", "Liner Notes")
        os.makedirs(notes)
        with open(os.path.join(notes, "notes.txt"), "wb") as handle:
            handle.write(b"text")

    SPECS = ((REMOTE, NOT_OFFERED), ("../secret", NOT_OFFERED),
             ("Metallica", OLD_TEXT), ("Metallica/Liner Notes/", NOTHING_PACKABLE),
             ("Metallica/Black Album (1991)/", too_large("4.9KB")))

    def test_request_only_sends_nothing_for_any_reason(self):
        for spec, _text in self.SPECS:
            with self.subTest(spec=spec):
                self.assertEqual(self.request(spec, channel="#r"), [])
                self.assertEqual(config.dcc_queue, {})
                self.assertEqual(self.sock.sent, [])

    def test_a_normal_channel_on_the_same_list_is_told(self):
        """Control: the same five requests in a Normal channel are each told
        their own reason, so the silence above is the mode and not an
        earlier bail-out."""
        for spec, text in self.SPECS:
            with self.subTest(spec=spec):
                self.assertEqual(self.request(spec, channel="#n"),
                                 [("dave", framed(text), True)])


class APackThatRaisesIsNotCalledAnArtistRoot(RefusalCase):
    """A pack that fails while it runs is not a refused request: the packer
    tells the requester through the queue's own failure path, never with the
    artist-root refusal, which named a reason that was not the one."""

    def test_a_pack_that_raises_sends_no_artist_root_notice(self):
        config.channel_users = {"#dccore-test": {"dave"}}
        config.bot_joined_channel = True
        config.dcc_queue["dave"] = [{
            "file": "Black_Album_(1991).rar", "path": self.tree.album,
            "channel": "#dccore-test", "user_raw": "dave",
            "is_unpacked_rar_folder": True, "is_temporary_zip": True,
        }]

        def failing_pack(*_a, **_k):
            raise OSError("stand-in pack failure")

        real = dcc.pack_folder_for_request
        dcc.pack_folder_for_request = failing_pack
        self.addCleanup(setattr, dcc, "pack_folder_for_request", real)
        self.oserve.queued.clear()

        with quiet():
            dcc.check_queue_and_send(self.sock, "dave")

        self.assertFalse([q for q in self.oserve.queued if OLD_TEXT in str(q[1])],
                         self.oserve.queued)


class TheTextForEachReason(unittest.TestCase):

    def test_each_reason(self):
        cases = {
            announce.PACK_REFUSED_ARTIST_ROOT: OLD_TEXT,
            announce.PACK_REFUSED_NOT_OFFERED: NOT_OFFERED,
            announce.PACK_REFUSED_NOTHING_PACKABLE: NOTHING_PACKABLE,
        }
        for reason, text in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(announce.pack_refusal_text(reason), text)

    def test_the_reasons_are_distinct(self):
        reasons = (announce.PACK_REFUSED_ARTIST_ROOT, announce.PACK_REFUSED_NOT_OFFERED,
                   announce.PACK_REFUSED_NOTHING_PACKABLE, announce.PACK_REFUSED_TOO_LARGE)
        self.assertEqual(len(set(reasons)), 4)
        self.assertEqual(len({announce.pack_refusal_text(r, 1024) for r in reasons}), 4)

    def test_the_default_is_the_old_text(self):
        self.assertEqual(announce.pack_refusal_text(), OLD_TEXT)

    def test_too_large_names_the_limit_in_readable_units(self):
        self.assertEqual(
            announce.pack_refusal_text(announce.PACK_REFUSED_TOO_LARGE, 10 * 1024 ** 3),
            too_large("10.0GB"))
        self.assertEqual(
            announce.pack_refusal_text(announce.PACK_REFUSED_TOO_LARGE, 1536 * 1024 ** 2),
            too_large("1.5GB"))

    def test_too_large_without_a_limit_names_no_number(self):
        expected = ("That folder is larger than the limit for packed folders. "
                    "Its files can still be requested one by one.")
        for limit in (None, 0):
            with self.subTest(limit=limit):
                text = announce.pack_refusal_text(announce.PACK_REFUSED_TOO_LARGE, limit)
                self.assertEqual(text, expected)
                self.assertFalse(any(ch.isdigit() for ch in text), text)

    def test_an_unknown_reason_gets_a_neutral_text(self):
        for reason in ("no_such_reason", None, ""):
            with self.subTest(reason=reason):
                self.assertEqual(announce.pack_refusal_text(reason),
                                 "That folder cannot be packed.")


class TheFrameIsTheGoldenOne(DCCoreTestCase):
    """Only the Error text moves: the frame and colours are byte-identical."""

    def setUp(self):
        super().setUp()
        self.set_config(THEME="classic")

    def sent(self, **kwargs):
        self.oserve.queued.clear()
        announce.send_pack_error_notice(None, "dave", **kwargs)
        return [message for _user, message, _vip in self.oserve.queued]

    def test_no_reason_is_the_golden_line(self):
        self.assertEqual(self.sent(), [GOLDEN_LINE])

    def test_every_reason_is_the_golden_frame_around_its_text(self):
        cases = (
            ({"reason": announce.PACK_REFUSED_ARTIST_ROOT}, OLD_TEXT),
            ({"reason": announce.PACK_REFUSED_NOT_OFFERED}, NOT_OFFERED),
            ({"reason": announce.PACK_REFUSED_NOTHING_PACKABLE}, NOTHING_PACKABLE),
            ({"reason": announce.PACK_REFUSED_TOO_LARGE, "limit_bytes": 10 * 1024 ** 3},
             too_large("10.0GB")),
            ({"reason": "no_such_reason"}, "That folder cannot be packed."),
        )
        for kwargs, text in cases:
            with self.subTest(**kwargs):
                self.assertEqual(self.sent(**kwargs), [framed(text)])

    def test_without_oserve_it_goes_to_the_socket(self):
        from tests.support import RecordingSocket
        sock = RecordingSocket()
        saved = sys.modules.pop("oserve")
        self.addCleanup(sys.modules.__setitem__, "oserve", saved)
        with contextlib.redirect_stdout(io.StringIO()):
            announce.send_pack_error_notice(sock, "dave",
                                            reason=announce.PACK_REFUSED_NOT_OFFERED)
        self.assertEqual(sock.text(), framed(NOT_OFFERED))


if __name__ == "__main__":
    unittest.main()
