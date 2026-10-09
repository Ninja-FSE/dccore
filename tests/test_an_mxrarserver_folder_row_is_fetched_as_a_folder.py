"""An mxrarserver folder row is fetched as a folder (#1209).

mxrarserver's folder list has no "!rar". Each row is the line to type,

    !<trigger> E:\\Music\\Some Artist\\Some Album.rar

and the bot packs that folder and sends it as a RAR. DCCore read such a row as
a FILE titled with the whole path: the request line was right, but the RAR
came back as "SomeAlbum.rar" - or "SomeAlbum-CD1.rar" for a folder named CD1
- and a file request admits only its own name, so it was refused as
unsolicited.

Now:

  * the row is offered as a folder ("Get folder as RAR"), the way a "!rar"
    row is - its rar_folder is the path;
  * it is asked for with the row's own text, no "!rar " in front, and as a
    "folder" request: the folder timeout and the folder size cap;
  * its RAR is admitted by the name mxrarserver gives it - the last folder,
    or "<parent> - <last>" when the last is generic (CD1, Disc 2, Vol III,
    Side A, Part 2, Covers...), with the spaces removed and \\ / : * ? < > |
    made "_" - compared without whitespace and underscores, case aside;
  * a lone pack row takes a RAR it did not predict, as a "!rar" row does,
    but never one of the bot's own list archives;
  * several may wait on one bot together, since names tell them apart,
    unless their names could clash.

Every nick, trigger and path here is invented.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc_fetch  # noqa: E402
import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

BOT = "SomeServer"
KEY = BOT.lower()
TRIGGER = "SomeTrigger"
ALBUM = "E:\\Music\\Some Artist\\Some Album.rar"
DISC = "E:\\Music\\Some Artist\\Some Album\\CD1.rar"


def names(path):
    return dcc_fetch.pack_offer_names(path)


class TheRow(unittest.TestCase):

    def rows(self, lines):
        folder = tempfile.mkdtemp(prefix="dccore-pack-")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        path = os.path.join(folder, "SomeServer-Folders(2)-MX.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        return list(list_mod.iter_filelist_rows(path, BOT))

    def test_a_folder_row_offers_its_path(self):
        row, = self.rows([f"!{TRIGGER} {ALBUM}"])

        self.assertEqual(row["title"], ALBUM)
        self.assertEqual(row["rar_folder"], ALBUM)

    def test_a_file_row_is_a_file(self):
        row, = self.rows([f"!{TRIGGER} Some Track.mp3 ::INFO:: 4.5 MB"])

        self.assertEqual(row["rar_folder"], "")

    def test_a_rar_file_with_a_size_is_a_file(self):
        row, = self.rows([f"!{TRIGGER} E:\\Music\\Old.rar ::INFO:: 40 MB"])

        self.assertEqual(row["rar_folder"], "")

    def test_a_rar_file_with_no_path_is_a_file(self):
        row, = self.rows([f"!{TRIGGER} Loose Archive.rar"])

        self.assertEqual(row["rar_folder"], "")

    def test_a_rar_row_is_unchanged(self):
        row, = self.rows([f"!{BOT} !rar D:\\MEDIA\\Some Album\\"])

        self.assertEqual(row["rar_folder"], "D:\\MEDIA\\Some Album\\")


class TheNameItArrivesUnder(unittest.TestCase):

    def test_the_last_folder(self):
        self.assertEqual(names(ALBUM), {"somealbum.rar"})

    def test_a_generic_last_folder_takes_its_parent(self):
        self.assertIn("somealbum-cd1.rar", names(DISC))
        # And the plain last name, which is what it sends with smart naming off.
        self.assertIn("cd1.rar", names(DISC))

    def test_every_generic_shape(self):
        for leaf in ("CD", "Disc", "disk", "Disco", "DVD", "LP", "Singles", "Cover", "Covers",
                     "Scan", "Scans", "Artwork", "CD1", "CD 01", "Disc 2", "Disk2", "Disco 3",
                     "Part 2", "Parte 4", "Vol 3", "Volume 12", "Volumen 2", "DVD1", "LP 2",
                     "CD II", "Vol III", "Disc x", "Side A", "SideB", "Lado A", "A Side", "b lado",
                     "Pt 2", "pt3", "Chapter IV", "Capitulo 7", "Session 01"):
            with self.subTest(leaf=leaf):
                found = names(f"E:\\Music\\Artist\\Parent Name\\{leaf}.rar")
                wanted = dcc_fetch._pack_key(f"Parent Name - {leaf}.rar")
                self.assertIn(wanted, found)

    def test_a_name_that_only_starts_like_one_is_not_generic(self):
        for leaf in ("CD1 Bonus", "Discography", "Volume", "Side C", "Covers Album", "Part",
                     "Chapter", "Vol XI"):
            with self.subTest(leaf=leaf):
                self.assertEqual(names(f"E:\\Music\\Parent Name\\{leaf}.rar"),
                                 {dcc_fetch._pack_key(f"{leaf}.rar")})

    def test_unsafe_characters_become_underscores(self):
        """A folder at the root of a drive: its parent is "E:"."""
        self.assertIn(dcc_fetch._pack_key("E_ - CD1.rar"), names("E:\\CD1.rar"))
        self.assertNotIn("e:-cd1.rar", names("E:\\CD1.rar"))


class Asking(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(fetch_queue={}, MAX_FETCH_SLOTS=10, fetch_feature_disabled=False,
                        CHANNEL="#somechannel", FETCH_MAX_PER_BOT=10)
        config.channel_users["#somechannel"] = {KEY, "someuser"}
        runtime.known_bots.clear()
        self.addCleanup(runtime.known_bots.clear)
        runtime.known_bots[KEY] = {"nick": BOT, "trigger": TRIGGER}

    def asked(self):
        return [msg for _user, msg, *_ in self.oserve.queued if "PRIVMSG" in msg]

    def ask(self, path=ALBUM):
        status, body = webserver.build_folder_rar_fetch_enqueue_result(BOT, path)
        self.assertEqual(status, 200, body)
        return config.fetch_queue[body["created"][0]]

    def test_it_is_asked_for_with_the_row_s_own_text(self):
        row = self.ask()
        dcc_fetch.check_fetch_queue()

        self.assertEqual(self.asked(), [f"PRIVMSG #somechannel :!{TRIGGER} {ALBUM}\r\n"])
        self.assertEqual((row["request_type"], row["requested_filename"]), ("folder", ALBUM))

    def test_a_rar_folder_still_gets_its_rar(self):
        status, body = webserver.build_folder_rar_fetch_enqueue_result(BOT, "D:\\MEDIA\\Album\\")

        self.assertEqual(config.fetch_queue[body["created"][0]]["requested_filename"],
                         "!rar D:\\MEDIA\\Album\\")

    def test_it_waits_the_folder_timeout_with_no_other_sign(self):
        runtime.known_bots[KEY] = {"nick": BOT}
        row = self.ask()

        self.assertEqual(dcc_fetch._offer_timeout_for(row, 60, 1800, 120), 1800)

    def test_a_ticked_or_pasted_row_goes_the_folder_route(self):
        status, body = webserver.build_fetch_enqueue_result(
            [{"bot": TRIGGER, "filename": ALBUM}, {"bot": BOT, "filename": "Some Track.mp3"}])

        self.assertEqual(status, 200, body)
        rows = [config.fetch_queue[rid] for rid in body["created"]]
        self.assertEqual([(r["request_type"], r["requested_filename"]) for r in rows],
                         [("folder", ALBUM), ("file", "Some Track.mp3")])
        self.assertEqual((rows[0]["bot"], rows[0]["trigger"]), (BOT, TRIGGER))

    def test_the_queue_itself_refuses_a_clash(self):
        """Not only the dashboard's route: enqueue_fetch() holds the rule
        under the lock that does the insert."""
        self.assertIsNotNone(dcc_fetch.enqueue_fetch(BOT, ALBUM, request_type="folder"))

        self.assertIsNone(dcc_fetch.enqueue_fetch(BOT, "F:\\Some Album.rar", request_type="folder"))
        self.assertIsNone(dcc_fetch.enqueue_fetch(BOT, "", request_type="list"))
        self.assertIsNotNone(dcc_fetch.enqueue_fetch(BOT, DISC, request_type="folder"))

    def test_two_folders_may_wait_together(self):
        self.ask(ALBUM)
        self.ask(DISC)

        self.assertEqual(len(config.fetch_queue), 2)

    def test_two_that_would_arrive_under_one_name_may_not(self):
        self.ask("E:\\Music\\Artist One\\Greatest Hits.rar")

        status, body = webserver.build_folder_rar_fetch_enqueue_result(
            BOT, "F:\\Other\\Artist Two\\Greatest Hits.rar")

        self.assertEqual(status, 409)
        self.assertEqual(body["error"], webserver.PACK_FETCH_CONFLICT_ERROR)

    def test_nor_beside_a_list_request(self):
        dcc_fetch.enqueue_fetch(BOT, "", request_type="list")

        status, _body = webserver.build_folder_rar_fetch_enqueue_result(BOT, ALBUM)

        self.assertEqual(status, 409)

    def test_nor_beside_a_rar_request(self):
        dcc_fetch.enqueue_fetch(BOT, "!rar Artist/Album", request_type="folder")

        status, _body = webserver.build_folder_rar_fetch_enqueue_result(BOT, ALBUM)

        self.assertEqual(status, 409)

    def test_and_a_list_or_rar_request_waits_for_it(self):
        self.ask()

        self.assertEqual(webserver.build_list_fetch_enqueue_result(BOT)[0], 409)
        self.assertEqual(webserver.build_folder_rar_fetch_enqueue_result(BOT, "D:\\MEDIA\\X\\")[0], 409)

    def test_another_bot_is_another_matter(self):
        self.ask()
        config.channel_users["#somechannel"].add("otherserver")

        status, _body = webserver.build_folder_rar_fetch_enqueue_result("OtherServer", ALBUM)

        self.assertEqual(status, 200)


class Admitting(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.set_config(fetch_queue={})

    def pack(self, path, bot=BOT, state="offered"):
        rid = dcc_fetch.enqueue_fetch(bot, path, request_type="folder")
        self.assertIsNotNone(rid)
        config.fetch_queue[rid].update(state=state, offered_at=time.time())
        return rid

    def claim(self, offered, bot=BOT):
        with dcc_fetch._fetch_lock():
            return dcc_fetch._claim_matching_offer_locked(config.fetch_queue, bot, offered)[0]

    def test_by_the_predicted_name(self):
        album = self.pack(ALBUM)
        disc = self.pack(DISC)

        self.assertEqual(self.claim("SomeAlbum-CD1.rar"), disc)
        self.assertEqual(self.claim("SomeAlbum.rar"), album)
        self.assertEqual(config.fetch_queue[album]["filename"], "SomeAlbum.rar")
        self.assertEqual(config.fetch_queue[album]["requested_filename"], ALBUM)

    def test_underscores_spaces_and_case_aside(self):
        album = self.pack(ALBUM)
        self.pack(DISC)

        self.assertEqual(self.claim("some_album.RAR"), album)

    def test_with_smart_naming_off(self):
        self.pack(ALBUM)
        disc = self.pack(DISC)

        self.assertEqual(self.claim("CD1.rar"), disc)

    def test_while_queued_there_too(self):
        album = self.pack(ALBUM, state="queued")

        self.assertEqual(self.claim("SomeAlbum.rar"), album)

    def test_a_lone_pack_row_takes_a_name_it_did_not_predict(self):
        album = self.pack(ALBUM)

        self.assertEqual(self.claim("Something_Else.rar"), album)

    def test_but_not_a_list_archive(self):
        self.pack(ALBUM)

        self.assertIsNone(self.claim("SomeServer-Files(1234)-MX.rar"))

    def test_nor_a_file_that_is_not_a_rar(self):
        self.pack(ALBUM)

        self.assertIsNone(self.claim("Some Track.mp3"))

    def test_two_waiting_and_a_name_neither_predicted_is_refused(self):
        self.pack(ALBUM)
        self.pack(DISC)

        self.assertIsNone(self.claim("Something_Else.rar"))

    def test_only_from_the_bot_asked(self):
        self.pack(ALBUM)

        self.assertIsNone(self.claim("SomeAlbum.rar", bot="SomeoneElse"))

    def test_a_late_list_still_reaches_its_row(self):
        """A list request that gave up, and a pack row waiting: the list's
        archive is the list's."""
        lid = dcc_fetch.enqueue_fetch(BOT, "", request_type="list")
        config.fetch_queue[lid].update(state="failed", reason="no response",
                                       offered_at=time.time() - 120)
        self.pack(ALBUM)

        self.assertEqual(self.claim("SomeServer-Folders(56)-MX.rar"), lid)

    def test_a_name_beats_the_bot_alone(self):
        """A queue the dashboard would not have built - restored from an
        older version, say - with a "!rar" row and a pack row for one bot: the
        pack row its RAR names still takes it."""
        rar = self.pack("!rar Artist/Album")
        config.fetch_queue[rar]["requested_at"] = time.time() - 100
        album = dcc_fetch.uuid.uuid4().hex[:12]
        config.fetch_queue[album] = dict(dcc_fetch.new_fetch_row(BOT, ALBUM, request_type="folder"),
                                         state="offered", offered_at=time.time())

        self.assertEqual(self.claim("SomeAlbum.rar"), album)
        self.assertEqual(self.claim("Anything.rar"), rar)

    def test_a_rar_row_is_admitted_on_the_bot_alone_as_before(self):
        rid = self.pack("!rar Artist/Album")

        self.assertEqual(self.claim("Anything At All.zip"), rid)

    def test_the_folder_size_cap_applies(self):
        """Admitted as a "folder" row, so handle_incoming_offer() holds it to
        MAX_FETCH_FOLDER_FILE_SIZE and _run_transfer() to the folder wall
        clock."""
        rid = self.pack(ALBUM)

        self.assertEqual(config.fetch_queue[rid]["request_type"], "folder")
        self.assertEqual(dcc_fetch._fetch_transfer_timeout("folder"),
                         getattr(config, "FETCH_FOLDER_TRANSFER_TIMEOUT", 6144))


if __name__ == "__main__":
    unittest.main()
