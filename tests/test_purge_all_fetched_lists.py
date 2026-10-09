"""webserver.build_purge_all_fetched_lists_result(), issue #1260.

A bot's list held from before #1232/#1240 never had a real channel of its
own on record, so the first ordinary re-fetch after upgrading resolved one
from a weaker fallback - and for a peer that advertises the same list
identically in several channels, whichever one that fallback happened to
land on got stamped and reused from then on, sometimes wrong. Found live: a
folder request (and its retry) both went out in a channel the peer bot
served nothing in, with no answer, because this had landed wrong once.

Purge-offline (#385, tests/test_purge_offline_fetched_lists.py) only ever
clears a bot showing the red "not here" dot. This clears every held list
regardless of status, so each one rebuilds from scratch with a channel
resolved clean - the one thing purge-offline cannot do for a bot that is
online right now, which is exactly the shape of the bot that triggered this.
"""

import io
import os
import sys
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import dcc_fetch  # noqa: E402
import list_fetch  # noqa: E402
import list_index  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

CHANNEL = "#somechannel"


def _held_entry(bot, fetched_at=1):
    return {"bot": bot, "fetched_at": fetched_at, "entry_count": 1,
            "advert_when_fetched": {}}


class ScopingTests(DCCoreTestCase):
    """Unlike purge-offline, online status never matters here."""

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL=CHANNEL)

    def test_nothing_held_means_nothing_to_purge(self):
        self.set_config(fetched_bot_lists={})

        status, result = webserver.build_purge_all_fetched_lists_result()

        self.assertEqual(status, 200)
        self.assertEqual(result["purged"], [])
        self.assertEqual(result["count"], 0)

    def test_a_bot_that_is_here_right_now_is_purged_too(self):
        """The whole point (#1260): purge-offline would never touch this
        one, and this bot being online is exactly the situation a stuck
        wrong channel needs a fresh fetch to correct."""
        config.channel_users[CHANNEL] = {"onlinebot"}
        self.set_config(fetched_bot_lists={"onlinebot": _held_entry("onlinebot")})

        status, result = webserver.build_purge_all_fetched_lists_result()

        self.assertEqual(status, 200)
        self.assertEqual(result["purged"], ["onlinebot"])
        self.assertNotIn("onlinebot", config.fetched_bot_lists)

    def test_an_offline_bot_is_purged_too(self):
        config.channel_users[CHANNEL] = {"someoneelse"}
        self.set_config(fetched_bot_lists={"offlinebot": _held_entry("offlinebot")})

        status, result = webserver.build_purge_all_fetched_lists_result()

        self.assertEqual(result["purged"], ["offlinebot"])

    def test_a_bot_still_joining_is_purged_too(self):
        """online is None (the grey dot) stops purge-offline; it changes
        nothing here - there is no online/offline distinction at all."""
        self.assertEqual(dict(config.channel_users), {})
        self.set_config(fetched_bot_lists={"somebot": _held_entry("somebot")})

        status, result = webserver.build_purge_all_fetched_lists_result()

        self.assertEqual(result["purged"], ["somebot"])
        self.assertNotIn("somebot", config.fetched_bot_lists)

    def test_every_held_bot_goes_in_one_call(self):
        config.channel_users[CHANNEL] = {"irrelevant"}
        self.set_config(fetched_bot_lists={
            "herebot": _held_entry("herebot"),
            "gonebot": _held_entry("gonebot"),
        })

        status, result = webserver.build_purge_all_fetched_lists_result()

        self.assertEqual(sorted(result["purged"]), ["gonebot", "herebot"])
        self.assertEqual(result["count"], 2)
        self.assertEqual(config.fetched_bot_lists, {})

    def test_the_bots_own_recorded_nick_is_reported_not_the_lower_cased_key(self):
        self.set_config(fetched_bot_lists={
            "loudbot": {"bot": "LoudBot", "fetched_at": 1, "entry_count": 1,
                        "advert_when_fetched": {}}})

        _status, result = webserver.build_purge_all_fetched_lists_result()

        self.assertEqual(result["purged"], ["LoudBot"])


class InFlightExemptionTests(DCCoreTestCase):
    """Same safety purge-offline already applies, and for the same reason -
    forgetting a bot's list while an answer for it is in flight is unsafe
    regardless of whether this is clearing one bot or every bot."""

    def setUp(self):
        super().setUp()
        self.set_config(fetched_bot_lists={"busybot": _held_entry("busybot")})

    def test_a_bot_with_a_pending_fetch_is_not_purged(self):
        dcc_fetch.enqueue_fetch("busybot", "", request_type="list")

        status, result = webserver.build_purge_all_fetched_lists_result()

        self.assertEqual(status, 200)
        self.assertEqual(result["purged"], [])
        self.assertIn("busybot", result["skipped_in_flight"])
        self.assertIn("busybot", config.fetched_bot_lists)

    def test_once_the_fetch_resolves_the_bot_is_purgeable_again(self):
        rid = dcc_fetch.enqueue_fetch("busybot", "Track.flac", request_type="file")
        with dcc_fetch._fetch_lock():
            config.fetch_queue[rid]["state"] = "complete"

        _status, result = webserver.build_purge_all_fetched_lists_result()

        self.assertEqual(result["purged"], ["busybot"])
        self.assertEqual(result["skipped_in_flight"], [])


class EndToEndPurgeTests(DCCoreTestCase):
    """A real fetch - files on disk, rows in the search index - for a bot
    that is ONLINE right now, cleared by the same route purge-offline could
    never reach for it."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-purge-all-e2e-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp
        self.set_config(CHANNEL=CHANNEL)
        config.channel_users[CHANNEL] = {"goneforgood"}

    def _seed_real_fetch(self, bot):
        zip_path = os.path.join(self.tmp, f"{bot}.zip")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(
                f"{bot}-2026-09-10.txt",
                "List of 1 Files generated on Sep 10th\n"
                "To request a file, copy/paste to the channel... !x FILENAME\n\n\n"
                + "=" * 53 + "\n"
                "Folder\\Path\\\n"
                f"!{bot} Track.flac  ::INFO:: 1.0MB\n")
        with open(zip_path, "wb") as fh:
            fh.write(buf.getvalue())
        ok, reason = list_fetch.process_fetched_list_zip(bot, zip_path)
        self.assertTrue(ok, reason)

    def test_purging_an_online_bots_real_fetch_removes_files_and_index_rows(self):
        self._seed_real_fetch("goneforgood")
        extract_dir = list_fetch.list_extract_dir("goneforgood")
        self.assertTrue(os.path.isdir(extract_dir))
        self.assertIn("goneforgood", list_index.indexed_bots())

        status, result = webserver.build_purge_all_fetched_lists_result()

        self.assertEqual(status, 200)
        self.assertEqual(result["purged"], ["goneforgood"])
        self.assertNotIn("goneforgood", config.fetched_bot_lists)
        self.assertFalse(os.path.exists(extract_dir))
        self.assertNotIn("goneforgood", list_index.indexed_bots())


if __name__ == "__main__":
    unittest.main()
