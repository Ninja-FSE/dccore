"""Four of the low-severity findings from the #162 audit, tracked on #177.

Unrelated to each other except in severity, so they are four classes rather
than one story. Grouped in one module because they were fixed in one pass, the
way the medium batch was.

    #22  irc.py            the bot registry grew without bound from
                           unauthenticated channel text
    #24  list_fetch.py     the one function in the module without long_path(),
                           raising out of a "never raises" contract
    #26  db.py             a stats.txt with the wrong column count loaded as
                           zeros, silently, and the next transfer persisted them
    #31  list.py,          three user-visible IRC lines that skipped the
         announce.py x2    420-byte budget every sibling line goes through
"""

import io
import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import defaults as config  # noqa: E402
import db  # noqa: E402
import irc  # noqa: E402
import list as list_mod  # noqa: E402
import list_fetch  # noqa: E402
import platform_compat  # noqa: E402
import runtime  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

T0 = 1_700_000_000.0

# A 158-character classical track name of the kind that actually lives on the
# NFS mount, and a Greek one where every letter costs two bytes.
CLASSICAL = (
    "Ludwig van Beethoven - Symphony No. 9 in D minor, Op. 125 'Choral' - "
    "IV. Finale - Presto - Allegro assai vivace - Alla marcia (Karajan 1963 DG).flac"
)
GREEK = "Ο Θεόδωρος Βασιλικός - Το τραγούδι του δρόμου που δεν τελειώνει ποτέ" * 3


# ---------------------------------------------------------------- #22
class TheBotRegistryIsBounded(DCCoreTestCase):
    """runtime.known_bots is filled from unauthenticated channel text: anyone
    can create an entry by advertising once under an unused nick, and a
    nick-change loop creates one per change. It is persisted, so it survived a
    restart too.

    Measured before the fix: 20,000 distinct nicks produced 20,000 entries,
    1.7 MB, and 29ms of json.dumps on every flush.

    The field to prune by was already there. _advert_tails, the sibling buffer
    written in the same capture path, has been time-pruned since it was
    written; this dict collected "last_seen" and never read it.
    """

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(KNOWN_BOTS_FILE=os.path.join(self.tree.root, "kb.json"))
        runtime.known_bots.clear()
        irc._advert_tails.clear()
        self.addCleanup(runtime.known_bots.clear)
        self.addCleanup(irc._advert_tails.clear)
        runtime.known_bots_flushed_at = T0

    def advertise(self, nick, now=T0):
        irc._capture_channel_advert(
            nick, "#dccore-test", f"Type: @{nick} For My List Of: 5 Files", now=now)

    def test_a_flood_of_one_off_nicks_cannot_grow_it_without_limit(self):
        for index in range(irc.KNOWN_BOTS_MAX + 500):
            self.advertise(f"n{index}")

        self.assertEqual(len(runtime.known_bots), irc.KNOWN_BOTS_MAX)

    def test_a_bot_not_seen_inside_the_ttl_is_forgotten(self):
        self.advertise("staleboy", now=T0 - irc.KNOWN_BOTS_TTL_SECONDS - 1)
        self.advertise("liveboy", now=T0)

        self.assertEqual(sorted(runtime.known_bots), ["liveboy"])

    def test_a_bot_seen_inside_the_ttl_is_kept(self):
        """The control. A sweep that forgot everything would pass the test
        above and make the registry useless."""
        self.advertise("recent", now=T0 - 60)
        self.advertise("liveboy", now=T0)

        self.assertEqual(sorted(runtime.known_bots), ["liveboy", "recent"])

    def test_eviction_drops_the_least_recently_seen_first(self):
        """The bots that actually advertise are the ones worth keeping.

        The keeper is named to sort FIRST and the flood to sort last, so an
        eviction that ordered by key instead of by last_seen would drop the
        keeper and this would fail. The obvious version of this test - a
        keeper called "regular" against a flood called "floodN" - passes
        either way, because "regular" happens to sort last.
        """
        self.advertise("aaa-real-server", now=T0)
        for index in range(irc.KNOWN_BOTS_MAX + 50):
            self.advertise(f"zzz-flood{index:05d}", now=T0 - 10)

        self.assertIn("aaa-real-server", runtime.known_bots,
                      "the long-standing bot was evicted before the flood")

    def test_an_entry_with_no_last_seen_is_treated_as_old(self):
        """An older file or a hand edit. last_seen is written on every single
        capture, so its absence means the entry is not being refreshed - not
        that it should be kept for ever."""
        runtime.known_bots["ancient"] = {"nick": "ancient", "files": 5}

        self.advertise("liveboy", now=T0)

        self.assertNotIn("ancient", runtime.known_bots)


# ---------------------------------------------------------------- #24
class ThePickedListFileSurvivesADeepPath(unittest.TestCase):
    """_pick_list_file() was the one function in list_fetch.py without
    long_path(), and both halves bite on Windows: os.walk() returns nothing
    for a directory past MAX_PATH, and getsize() raises FileNotFoundError out
    of a function its caller documents as never raising.

    The depth is not this bot's to control - a fetched list lands under a temp
    directory plus the sending bot's nick plus whatever it called its file.
    """

    def setUp(self):
        import tempfile
        self.root = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        import shutil
        shutil.rmtree(platform_compat.long_path(self.root), ignore_errors=True)

    def deep_dir(self, target_length=300):
        path = self.root
        while len(path) < target_length:
            path = os.path.join(path, "a-fairly-long-directory-name-segment")
        os.makedirs(platform_compat.long_path(path), exist_ok=True)
        return path

    def write(self, path, body):
        with io.open(platform_compat.long_path(path), "w", encoding="utf-8") as handle:
            handle.write(body)

    def test_a_list_past_max_path_is_still_found(self):
        deep = self.deep_dir()
        self.assertGreater(len(deep), 260, "the fixture is not actually deep")
        self.write(os.path.join(deep, "TheirBot-2026-08-31.txt"), "!x Song.flac\n" * 50)

        found = list_fetch._pick_list_file(self.root)

        self.assertIsNotNone(found, "the deep list file was invisible to the walk")
        self.assertTrue(os.path.basename(found).startswith("TheirBot"))

    def test_sizing_works_on_a_deep_path_too(self):
        """The walk and the SIZING are two separate long_path() call sites,
        and only the sizing runs when there is more than one candidate - a
        single-file fixture returns before ever measuring anything, so it
        cannot tell the two apart."""
        deep = self.deep_dir()
        self.write(os.path.join(deep, "TheirBot-2026-08-30.txt"), "x" * 10)
        self.write(os.path.join(deep, "TheirBot-2026-08-31.txt"), "x" * 5000)

        found = list_fetch._pick_list_file(self.root)

        self.assertIsNotNone(found)
        self.assertEqual(os.path.basename(found), "TheirBot-2026-08-31.txt",
                         "the larger candidate should win, so sizing ran")

    def test_a_member_that_vanishes_mid_sort_does_not_raise(self):
        """_extract_and_locate_list_file documents this as never raising. An
        antivirus quarantine between the walk and the sizing is the realistic
        way it happens."""
        self.write(os.path.join(self.root, "a.txt"), "x" * 10)
        self.write(os.path.join(self.root, "b.txt"), "x" * 999)
        real_getsize = os.path.getsize

        def vanishing(path):
            if str(path).endswith("a.txt"):
                raise FileNotFoundError(2, "quarantined")
            return real_getsize(path)

        os.path.getsize = vanishing
        self.addCleanup(setattr, os.path, "getsize", real_getsize)

        found = list_fetch._pick_list_file(self.root)

        self.assertEqual(os.path.basename(found), "b.txt",
                         "the readable candidate should still win")

    def test_the_largest_candidate_still_wins_normally(self):
        """The control - the sizing must still actually sort."""
        self.write(os.path.join(self.root, "small.txt"), "x" * 10)
        self.write(os.path.join(self.root, "big.txt"), "x" * 5000)

        self.assertEqual(os.path.basename(list_fetch._pick_list_file(self.root)),
                         "big.txt")


# ---------------------------------------------------------------- #26
class ACorruptStatsFileIsKeptAndReported(DCCoreTestCase):
    """A stats.txt with any column count but 7 or the legacy 2 fell through to
    all-zero defaults with no log line at all, and the next completed transfer
    persisted the zeros - the lifetime totals gone, and nothing to say when.

    load_dcc_queue() has preserved a .corrupt copy since it was written. This
    loader never did, so the one artefact that could have said what the totals
    used to be was destroyed by the next transfer.
    """

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(STATS_FILE=os.path.join(self.tree.root, "stats.txt"))
        db._advanced_stats_cache = None

    def write(self, body):
        with io.open(config.STATS_FILE, "w", encoding="utf-8") as handle:
            handle.write(body)
        db._advanced_stats_cache = None

    def corrupt_copy(self):
        return config.STATS_FILE + ".corrupt"

    def test_a_wrong_column_count_is_preserved(self):
        self.write("1 2 3 4")

        db.load_advanced_stats()

        self.assertTrue(os.path.exists(self.corrupt_copy()),
                        "the damaged file was destroyed instead of kept")

    def test_the_preserved_copy_holds_what_was_there(self):
        self.write("1 2 3 4")

        db.load_advanced_stats()

        with io.open(self.corrupt_copy(), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "1 2 3 4")

    def test_it_says_so_rather_than_starting_from_zero_in_silence(self):
        import contextlib
        self.write("1 2 3 4")
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            db.load_advanced_stats()

        printed = buffer.getvalue()
        self.assertIn("stats.txt", printed)
        self.assertIn("found 4", printed, "the message does not say what was wrong")

    def test_non_numeric_columns_are_preserved_too(self):
        self.write("a b c d e f g")

        db.load_advanced_stats()

        self.assertTrue(os.path.exists(self.corrupt_copy()))

    def test_the_handle_is_closed_before_the_rename(self):
        """Renaming a file this process still has open raises PermissionError
        on Windows - which is finding #25 in the same audit. The first version
        of this fix committed it, and only running it showed up."""
        self.write("1 2 3 4")

        db.load_advanced_stats()

        self.assertFalse(os.path.exists(config.STATS_FILE),
                         "the original is still there, so the rename failed")

    def test_a_good_row_is_left_alone(self):
        self.write("100 200 3 4 5 6 2026-08-31")

        row = db.load_advanced_stats()

        self.assertEqual(row[:2], [100, 200])
        self.assertFalse(os.path.exists(self.corrupt_copy()))

    def test_the_legacy_two_column_row_is_still_supported(self):
        """An old build wrote only the two lifetime totals. That is a
        supported shape, not a corruption."""
        self.write("100 200")

        row = db.load_advanced_stats()

        self.assertEqual(row[:2], [100, 200])
        self.assertFalse(os.path.exists(self.corrupt_copy()))


# ---------------------------------------------------------------- #31
class EveryUserVisibleLineFitsTheBudget(DCCoreTestCase):
    """Three call sites skipped announce.fit_irc_line while their own siblings
    used it: the search RESULT rows (whose header uses it, two lines above),
    the "Sending:" notice, and the queue-position notice.

    Content is operator-owned library filenames, never attacker-injectable -
    but the server's cut lands inside a colour code and the trailing reset is
    what gets discarded, so the background colour smears down the rest of the
    reader's window.
    """

    def setUp(self):
        super().setUp()
        self.addCleanup(setattr, announce, "send_debug", announce.send_debug)

    def sent(self):
        return [message for _user, message, _vip in self.oserve.queued]

    def assert_fits(self, line, label):
        size = len(line.encode("utf-8"))
        self.assertLessEqual(size, announce.IRC_LINE_BUDGET,
                             f"{label}: {size} bytes against a "
                             f"{announce.IRC_LINE_BUDGET} budget")

    def test_the_sending_notice_fits(self):
        for label, name in (("classical", CLASSICAL), ("greek", GREEK)):
            with self.subTest(name=label):
                self.oserve.queued.clear()
                announce.send_dcc_sending_notice("dave", name)

                self.assert_fits(self.sent()[-1], "send_dcc_sending_notice")

    def test_the_queue_notice_fits(self):
        for label, name in (("classical", CLASSICAL), ("greek", GREEK)):
            with self.subTest(name=label):
                self.oserve.queued.clear()
                announce.send_dcc_queue_notice("dave", name, 3)

                self.assert_fits(self.sent()[-1], "send_dcc_queue_notice")

    def test_a_short_name_is_not_clamped(self):
        """The control. Routing through fit_irc_line must not start putting an
        ellipsis on lines that were always fine."""
        self.oserve.queued.clear()
        announce.send_dcc_sending_notice("dave", "01 - Enter Sandman.flac")
        line = self.sent()[-1]

        self.assertIn("01 - Enter Sandman.flac", line)
        self.assertNotIn("...", line)

    def test_every_sender_that_takes_a_name_fits_the_budget(self):
        """The general property, driven rather than grepped.

        Every outbound path that interpolates a filename or a search term is
        called with a name long enough to blow the budget on its own. Counting
        fit_irc_line call sites in the source would pass just as happily on a
        sender that called it and ignored the result.
        """
        senders = {
            "send_transfer_complete":
                lambda name: announce.send_transfer_complete(
                    "#dccore-test", "dave", name, 4096, time.time() - 2, 512000),
            "send_dcc_sending_notice":
                lambda name: announce.send_dcc_sending_notice("dave", name),
            "send_dcc_queue_notice":
                lambda name: announce.send_dcc_queue_notice("dave", name, 3),
            "send_search_result_header":
                lambda name: announce.send_search_result_header(
                    "dave", name, 3, "#dccore-test"),
        }
        from tests.support import silence_debug
        silence_debug(announce)

        for sender, call in senders.items():
            for label, name in (("classical", CLASSICAL), ("greek", GREEK)):
                with self.subTest(sender=sender, name=label):
                    self.oserve.queued.clear()
                    call(name)

                    self.assertTrue(self.sent(), f"{sender} queued nothing")
                    for line in self.sent():
                        self.assert_fits(line, sender)


if __name__ == "__main__":
    unittest.main()
