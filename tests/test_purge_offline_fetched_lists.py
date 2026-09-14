"""webserver.build_purge_offline_fetched_lists_result(), issue #385.

The List Browser has never forgotten a bot's fetched list on its own - once
fetched, an entry in config.fetched_bot_lists sat there forever, even for a
bot that will never reconnect. #385 asked for a way to clear exactly the
rows the sidebar already marks with the red "not in a channel" dot, without
touching a bot that is here now or one the daemon has not finished checking
yet, and without touching a bot a reply is still in flight for.

WHAT COUNTS AS "RED"

build_fetched_bot_list_summaries() (tests/test_list_browser_sidebar.py,
tests/test_we_do_not_ask_a_bot_that_is_not_there.py) already defines the
three states from config.channel_users via present_nicks():

    online is True   the bot is in a channel with us right now   (green)
    online is False  present_nicks() answered and it was absent  (red)
    online is None   the membership mirror is still empty        (grey)

The purge is scoped to exactly the middle one. A green bot is never a
candidate - forgetting a list still in use would be its own bug. A grey bot
is left alone too: an empty mirror is "nobody has looked yet", not "this bot
is gone", and treating it as gone would purge everything on every restart,
before a single JOIN has even been confirmed.
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
    """A minimal config.fetched_bot_lists entry - enough for the purge logic
    to see the bot as held, without a real fetch on disk. Whether the entry
    is real or synthetic is exactly what ForgetBotTests (tests/
    test_list_fetch.py) and EndToEndPurgeTests below exist to tell apart:
    this helper is for the routing/scoping tests, which only care which bots
    get PICKED, not what forgetting one actually removes.
    """
    return {"bot": bot, "fetched_at": fetched_at, "entry_count": 1,
            "advert_when_fetched": {}}


class ScopingTests(DCCoreTestCase):
    """Which bots the purge picks - the online/offline/unknown split."""

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL=CHANNEL)

    def test_nothing_held_means_nothing_to_purge(self):
        self.set_config(fetched_bot_lists={})

        status, result = webserver.build_purge_offline_fetched_lists_result()

        self.assertEqual(status, 200)
        self.assertEqual(result["purged"], [])
        self.assertEqual(result["count"], 0)

    def test_a_bot_showing_the_red_dot_is_purged(self):
        config.channel_users[CHANNEL] = {"onlinebot"}
        self.set_config(fetched_bot_lists={"offlinebot": _held_entry("offlinebot")})

        status, result = webserver.build_purge_offline_fetched_lists_result()

        self.assertEqual(status, 200)
        self.assertEqual(result["purged"], ["offlinebot"])
        self.assertEqual(result["count"], 1)
        self.assertNotIn("offlinebot", config.fetched_bot_lists)

    def test_a_bot_that_is_here_now_is_never_touched(self):
        config.channel_users[CHANNEL] = {"onlinebot"}
        self.set_config(fetched_bot_lists={"onlinebot": _held_entry("onlinebot")})

        status, result = webserver.build_purge_offline_fetched_lists_result()

        self.assertEqual(status, 200)
        self.assertEqual(result["purged"], [])
        self.assertIn("onlinebot", config.fetched_bot_lists)

    def test_still_joining_is_left_alone_not_treated_as_offline(self):
        """An empty channel_users mirror makes every bot read online=None,
        the grey dot - "cannot tell yet", not "gone". A purge run right
        after startup, before the first JOIN is confirmed, must not wipe out
        every held list on that coincidence alone."""
        self.assertEqual(dict(config.channel_users), {})
        self.set_config(fetched_bot_lists={"somebot": _held_entry("somebot")})

        status, result = webserver.build_purge_offline_fetched_lists_result()

        self.assertEqual(status, 200)
        self.assertEqual(result["purged"], [])
        self.assertIn("somebot", config.fetched_bot_lists)

    def test_a_mix_purges_only_the_red_ones(self):
        config.channel_users[CHANNEL] = {"herebot"}
        self.set_config(fetched_bot_lists={
            "herebot": _held_entry("herebot"),
            "gonebot": _held_entry("gonebot"),
            "longgonebot": _held_entry("longgonebot"),
        })

        status, result = webserver.build_purge_offline_fetched_lists_result()

        self.assertEqual(status, 200)
        self.assertEqual(sorted(result["purged"]), ["gonebot", "longgonebot"])
        self.assertEqual(result["count"], 2)
        self.assertIn("herebot", config.fetched_bot_lists)
        self.assertNotIn("gonebot", config.fetched_bot_lists)
        self.assertNotIn("longgonebot", config.fetched_bot_lists)

    def test_the_bots_own_recorded_nick_is_reported_not_the_lower_cased_key(self):
        """config.fetched_bot_lists is keyed lower-case, but "bot" inside the
        entry is the real casing the advert used - the same distinction
        build_fetched_bot_list_summaries() already makes. The purge result
        should read like the sidebar, not like a dict dump."""
        config.channel_users[CHANNEL] = {"someoneelse"}
        self.set_config(fetched_bot_lists={
            "loudbot": {"bot": "LoudBot", "fetched_at": 1, "entry_count": 1,
                        "advert_when_fetched": {}}})

        _status, result = webserver.build_purge_offline_fetched_lists_result()

        self.assertEqual(result["purged"], ["LoudBot"])


class InFlightExemptionTests(DCCoreTestCase):
    """A bot with a request outstanding is skipped even while showing red -
    forgetting it would delete the entry (or extract directory) a reply in
    flight is about to be matched against. See dcc_fetch.
    has_any_outstanding_request()'s docstring for the failure this avoids.
    """

    def setUp(self):
        super().setUp()
        self.set_config(CHANNEL=CHANNEL)
        config.channel_users[CHANNEL] = {"someotherbot"}
        self.set_config(fetched_bot_lists={"busybot": _held_entry("busybot")})

    def test_a_bot_with_a_pending_list_fetch_is_not_purged(self):
        dcc_fetch.enqueue_fetch("busybot", "", request_type="list")

        status, result = webserver.build_purge_offline_fetched_lists_result()

        self.assertEqual(status, 200)
        self.assertEqual(result["purged"], [])
        self.assertIn("busybot", result["skipped_in_flight"])
        self.assertIn("busybot", config.fetched_bot_lists)

    def test_a_bot_with_a_pending_file_fetch_is_not_purged(self):
        """Not just the bot-alone types - has_any_outstanding_request() is
        deliberately wider than has_outstanding_bot_alone_request()."""
        dcc_fetch.enqueue_fetch("busybot", "Track.flac", request_type="file")

        _status, result = webserver.build_purge_offline_fetched_lists_result()

        self.assertEqual(result["purged"], [])
        self.assertIn("busybot", result["skipped_in_flight"])

    def test_once_the_fetch_resolves_the_bot_is_purgeable_again(self):
        rid = dcc_fetch.enqueue_fetch("busybot", "Track.flac", request_type="file")
        with dcc_fetch._fetch_lock():
            config.fetch_queue[rid]["state"] = "complete"

        _status, result = webserver.build_purge_offline_fetched_lists_result()

        self.assertEqual(result["purged"], ["busybot"])
        self.assertEqual(result["skipped_in_flight"], [])


class EndToEndPurgeTests(DCCoreTestCase):
    """The scoping above is checked against synthetic entries; this class
    checks that a REAL fetch - files on disk, rows in the search index - is
    actually cleared by the same route, not just the dict key. Review on
    #385 flagged the index specifically: a purge that drops the registry
    entry but leaves the indexed rows behind is not a correctness bug
    (search_index() is already restricted to bots currently held) but a
    disk-space one, since the index is roughly as big again as the lists
    themselves.
    """

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-purge-e2e-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp
        self.set_config(CHANNEL=CHANNEL)
        # present_nicks() is only non-empty once at least one channel has a
        # confirmed membership - an empty channel_users dict (or one mapping
        # to only empty sets) reads as "still joining" (online: None), not
        # "confirmed absent" (online: False). A real nick here is what makes
        # the fetched bot below actually show the red dot these tests purge.
        config.channel_users[CHANNEL] = {"someoneelse"}

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

    def test_purging_a_real_fetch_removes_the_files_and_the_index_rows(self):
        self._seed_real_fetch("goneforgood")
        extract_dir = list_fetch.list_extract_dir("goneforgood")
        self.assertTrue(os.path.isdir(extract_dir))
        self.assertIn("goneforgood", list_index.indexed_bots())

        status, result = webserver.build_purge_offline_fetched_lists_result()

        self.assertEqual(status, 200)
        self.assertEqual(result["purged"], ["goneforgood"])
        self.assertNotIn("goneforgood", config.fetched_bot_lists)
        self.assertFalse(os.path.exists(extract_dir))
        self.assertNotIn("goneforgood", list_index.indexed_bots())


if __name__ == "__main__":
    unittest.main()
