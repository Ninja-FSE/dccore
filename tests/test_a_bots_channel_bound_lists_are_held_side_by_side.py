"""#1240: a bot that binds a different list to each of several channels we
share with it (DCCore's own multi-list-per-channel feature, which another
DCCore-family bot can equally run) gets EVERY one of those lists held and
browsable, not just whichever channel happened to answer first.

This is the fix for a real incident found while reviewing #1239: asking for a
bot's list from a channel other than the one already on record replaced the
bot's entire held entry with whatever that one channel answered - on a bot
with Main, RAR and a channel-bound second list, the second list's answer
silently deleted Main and RAR. The explicit-channel route that could trigger
it was reverted (#1239's final version); this issue is the real fix:
`_install_fetched_list()` now MERGES a channel's own markers into the bot's
existing entry rather than replacing the whole thing, and extracts a
secondary channel's archive into its own subdirectory so it can never
overwrite what is already on disk for another channel either.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import defaults as config  # noqa: E402
import list_fetch  # noqa: E402
import list_index  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_list_fetch import _write_zip, _list_txt  # noqa: E402

BOT = "SomeServer"
KEY = "someserver"


class TheIncidentItself(DCCoreTestCase):
    """The exact shape of what went wrong for real: Main + RAR already held,
    a request to the bot's OTHER channel answers with only its video content."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-1240-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp
        self.music_zip = os.path.join(self.tmp, "music.zip")
        self.video_zip = os.path.join(self.tmp, "video.zip")

    def fetch_music(self):
        _write_zip(self.music_zip, [
            (f"{BOT}-2026-08-27.txt", _list_txt(base_name=BOT, files=(("Song.flac", "10.0MB"),))),
            (f"{BOT}-RAR-2026-08-27.txt", _list_txt(base_name=BOT, files=(("Album.rar", "500.0MB"),))),
        ])
        return list_fetch.process_fetched_list_zip(BOT, self.music_zip, channel="#music")

    def fetch_video(self, filename="Clip.mkv"):
        _write_zip(self.video_zip, [
            (f"{BOT}-VIDEO-2026-08-27.txt", _list_txt(base_name=BOT, files=((filename, "700.0MB"),))),
        ])
        # secondary=True (#1240 review): this helper always plays the part of
        # list_grab.secondary_channel_tick()'s own confirmed fetch, the one
        # caller allowed to say so - never inferred from the channel here
        # differing from "#music" above.
        return list_fetch.process_fetched_list_zip(BOT, self.video_zip, channel="#video", secondary=True)

    def test_main_and_rar_survive_a_fetch_from_the_other_channel(self):
        ok, _reason = self.fetch_music()
        self.assertTrue(ok)
        before = dict(config.fetched_bot_lists[KEY]["lists"])

        ok, _reason = self.fetch_video()
        self.assertTrue(ok, _reason)

        after = config.fetched_bot_lists[KEY]["lists"]
        self.assertEqual(after[""], before[""], "the main (music) list must be untouched")
        self.assertEqual(after["RAR"], before["RAR"], "the RAR list must be untouched")

    def test_the_video_channels_list_is_added_as_its_own_marker(self):
        self.fetch_music()
        ok, _reason = self.fetch_video()
        self.assertTrue(ok, _reason)

        lists = config.fetched_bot_lists[KEY]["lists"]
        self.assertIn("video", lists)
        self.assertEqual(lists["video"]["channel"], "#video")
        self.assertEqual(lists["video"]["entry_count"], 1)
        rows = list_fetch.get_fetched_bot_page(
            {**config.fetched_bot_lists[KEY], **lists["video"]}, 0, 10)[0]
        self.assertIn("Clip.mkv", str(rows))

    def test_the_primary_channel_and_marker_are_unchanged(self):
        self.fetch_music()
        self.fetch_video()
        entry = config.fetched_bot_lists[KEY]
        self.assertEqual(entry["channel"], "#music")
        self.assertEqual(entry["lists"][""]["channel"], "#music")
        self.assertEqual(entry["lists"]["RAR"]["channel"], "#music")

    def test_the_video_list_is_searchable_under_its_own_name(self):
        self.fetch_music()
        self.fetch_video()
        results = list_index.search(["Clip"], bots=[f"{BOT}/video"])
        self.assertTrue(results, "the video list must be reachable by its index key")

    def test_refetching_video_again_replaces_only_the_video_marker(self):
        self.fetch_music()
        self.fetch_video(filename="First.mkv")
        before_main = dict(config.fetched_bot_lists[KEY]["lists"][""])
        before_rar = dict(config.fetched_bot_lists[KEY]["lists"]["RAR"])

        ok, _reason = self.fetch_video(filename="Second.mkv")
        self.assertTrue(ok, _reason)

        lists = config.fetched_bot_lists[KEY]["lists"]
        self.assertEqual(lists[""], before_main)
        self.assertEqual(lists["RAR"], before_rar)
        rows = list_fetch.get_fetched_bot_page(
            {**config.fetched_bot_lists[KEY], **lists["video"]}, 0, 10)[0]
        self.assertIn("Second.mkv", str(rows))
        self.assertNotIn("First.mkv", str(rows))

    def test_refetching_the_primary_channel_leaves_the_video_marker_alone(self):
        """The direction that matters just as much: an ordinary refresh of
        the bot's usual channel must not wipe out a secondary channel's list
        either.

        #1240 REVIEW: this used to check only the stored dict, which is
        exactly how a confirmed bug slipped through - the dict survived
        untouched (it lives in config.fetched_bot_lists, never written by an
        ordinary refresh of a DIFFERENT channel) while the FILE on disk the
        marker's "list_path" pointed at was deleted outright, because the
        old secondary directory lived INSIDE the primary's own extract_dir,
        which an ordinary refresh renames aside and then rmtree's whole on
        success. Opening the list for real, the way get_fetched_bot_page()
        and a dashboard click both do, is the only way that would have
        caught it."""
        self.fetch_music()
        self.fetch_video()
        video_before = dict(config.fetched_bot_lists[KEY]["lists"]["video"])

        ok, _reason = self.fetch_music()
        self.assertTrue(ok, _reason)

        self.assertEqual(config.fetched_bot_lists[KEY]["lists"]["video"], video_before)
        rows = list_fetch.get_fetched_bot_page(
            {**config.fetched_bot_lists[KEY], **config.fetched_bot_lists[KEY]["lists"]["video"]}, 0, 10)[0]
        self.assertIn("Clip.mkv", str(rows), "the video marker's file must still be readable on disk")

    def test_an_empty_or_unparseable_secondary_answer_changes_nothing(self):
        self.fetch_music()
        self.fetch_video()
        before = dict(config.fetched_bot_lists[KEY]["lists"])

        _write_zip(self.video_zip, [(f"{BOT}-VIDEO-2026-08-27.txt", "not a file list at all\n")])
        ok, reason = list_fetch.process_fetched_list_zip(BOT, self.video_zip, channel="#video", secondary=True)

        self.assertFalse(ok)
        self.assertIn("video", reason.lower())
        self.assertEqual(config.fetched_bot_lists[KEY]["lists"], before)

    def test_the_secondary_channel_extracts_outside_the_primarys_own_directory(self):
        self.fetch_music()
        primary_dir = list_fetch.list_extract_dir(BOT)
        primary_files_before = set(os.listdir(primary_dir))

        self.fetch_video()

        # The primary channel's own directory is not touched AT ALL (#1240
        # review) - the secondary channel's fetch extracts entirely outside
        # it now, not merely into an added subdirectory of it (an earlier
        # version's "_channels" entry, still vulnerable to the primary's own
        # hold-aside-and-rmtree cycle - see the directory test below).
        self.assertEqual(set(os.listdir(primary_dir)), primary_files_before,
                         "the primary channel's own directory must be untouched")
        secondary_dir = list_fetch.secondary_channel_extract_dir(BOT, "#video")
        self.assertTrue(os.path.isdir(secondary_dir))
        self.assertTrue(os.listdir(secondary_dir))


class AChannelsNameNeverCollidesWithAnotherMarkers(DCCoreTestCase):
    """#1240 review, confirmed bug: a channel's sanitised name can collide
    with a marker name already in use by something else entirely - a
    filename-derived marker from the primary's own archive (literally
    "#RAR" next to a primary that already has its own "RAR" list), or
    another secondary channel's own sanitised name (two different spellings
    that happen to sanitise the same way). Left uncaught, the second to
    arrive silently overwrites the first - in the dict, and in the search
    index, which folds case away regardless of what the dict keys look
    like."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-1240-collision-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp

    def test_a_channel_literally_named_rar_does_not_overwrite_the_primarys_own_rar_list(self):
        music_zip = os.path.join(self.tmp, "music.zip")
        _write_zip(music_zip, [
            (f"{BOT}-2026-08-27.txt", _list_txt(base_name=BOT, files=(("Song.flac", "10.0MB"),))),
            (f"{BOT}-RAR-2026-08-27.txt", _list_txt(base_name=BOT, files=(("Album.rar", "500.0MB"),))),
        ])
        ok, reason = list_fetch.process_fetched_list_zip(BOT, music_zip, channel="#music")
        self.assertTrue(ok, reason)

        rar_zip = os.path.join(self.tmp, "rar_channel.zip")
        _write_zip(rar_zip, [(f"{BOT}-2026-08-27.txt",
                              _list_txt(base_name=BOT, files=(("Other.mkv", "700.0MB"),)))])
        ok, reason = list_fetch.process_fetched_list_zip(BOT, rar_zip, channel="#RAR", secondary=True)
        self.assertTrue(ok, reason)

        lists = config.fetched_bot_lists[KEY]["lists"]
        self.assertEqual(lists["RAR"]["channel"], "#music",
                         "the primary's own RAR list must survive untouched")
        self.assertEqual(lists["RAR"]["entry_count"], 1)
        channel_markers = [m for m, info in lists.items()
                           if isinstance(info, dict) and info.get("channel") == "#RAR"]
        self.assertEqual(len(channel_markers), 1)
        self.assertNotEqual(channel_markers[0], "RAR",
                            "the #RAR channel's own marker must not collide with the filename one")
        self.assertEqual(lists[channel_markers[0]]["entry_count"], 1)

    def test_two_channels_that_sanitise_to_the_same_name_both_survive(self):
        one_zip = os.path.join(self.tmp, "one.zip")
        _write_zip(one_zip, [(f"{BOT}-2026-08-27.txt",
                              _list_txt(base_name=BOT, files=(("First.mkv", "1.0MB"),)))])
        ok, reason = list_fetch.process_fetched_list_zip(BOT, one_zip, channel="#a|b", secondary=True)
        self.assertTrue(ok, reason)

        two_zip = os.path.join(self.tmp, "two.zip")
        _write_zip(two_zip, [(f"{BOT}-2026-08-27.txt",
                              _list_txt(base_name=BOT, files=(("Second.mkv", "2.0MB"),)))])
        ok, reason = list_fetch.process_fetched_list_zip(BOT, two_zip, channel="#a_b", secondary=True)
        self.assertTrue(ok, reason)

        lists = config.fetched_bot_lists[KEY]["lists"]
        by_channel = {info.get("channel"): (marker, info) for marker, info in lists.items()
                     if isinstance(info, dict) and info.get("channel") in ("#a|b", "#a_b")}
        self.assertEqual(len(by_channel), 2, "both channels' markers must survive, under different keys")
        self.assertNotEqual(by_channel["#a|b"][0], by_channel["#a_b"][0])
        self.assertEqual(by_channel["#a|b"][1]["entry_count"], 1)
        self.assertEqual(by_channel["#a_b"][1]["entry_count"], 1)


class TheArrivedMessageReportsTheRealCount(DCCoreTestCase):
    """A real incident, hit live: a channel's own archive can sub-split
    inside itself - its base file (what _channel_marker_name(channel) alone
    would look up) turned out empty and was dropped, while a sibling file in
    the SAME archive (stored as "<channel>-VIDEO") held the real content.
    The console reported "0 files" for a fetch that genuinely brought back
    thousands, because the old lookup only ever checked the one key named
    after the channel itself.
    """

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-1240-arrived-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp
        self.music_zip = os.path.join(self.tmp, "music.zip")
        self.video_zip = os.path.join(self.tmp, "video.zip")
        self.events = []
        real = announce.feed_event
        announce.feed_event = lambda kind, text, **fields: self.events.append((kind, text, fields))
        self.addCleanup(setattr, announce, "feed_event", real)

    def arrived_text(self):
        for kind, text, fields in self.events:
            if kind == "LISTFETCH" and fields.get("action") == "arrived":
                return text
        return None

    def test_a_channel_whose_own_main_file_is_empty_still_reports_its_real_count(self):
        _write_zip(self.music_zip, [
            (f"{BOT}-2026-08-27.txt", _list_txt(base_name=BOT, files=(("Song.flac", "10.0MB"),))),
        ])
        list_fetch.process_fetched_list_zip(BOT, self.music_zip, channel="#music")
        self.events.clear()

        # The channel's own archive: an empty base file (just headers, no
        # request lines) alongside a real "-VIDEO-" one - exactly the shape
        # that left nothing stored under the base channel marker name.
        empty_header = "List of 0 Files generated on Jan 1st\n"
        _write_zip(self.video_zip, [
            (f"{BOT}-2026-08-27.txt", empty_header),
            (f"{BOT}-VIDEO-2026-08-27.txt", _list_txt(base_name=BOT, files=(("Clip.mkv", "700.0MB"),))),
        ])
        ok, _reason = list_fetch.process_fetched_list_zip(BOT, self.video_zip, channel="#video", secondary=True)

        self.assertTrue(ok)
        self.assertNotIn("video", config.fetched_bot_lists[KEY]["lists"])
        self.assertIn("video-VIDEO", config.fetched_bot_lists[KEY]["lists"])
        self.assertEqual(self.arrived_text(), f"{BOT}'s list arrived: 1 files")


class TheAdvertSignatureIsStamped(DCCoreTestCase):
    """Each marker remembers what its own channel was advertising when it was
    fetched (#1240) - list_grab._secondary_channel_candidates() reads this to
    know when an already-discovered secondary list has moved on."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-1240-sig-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp
        self.zip_path = os.path.join(self.tmp, "x.zip")

    def test_the_primary_markers_carry_the_channels_live_signature(self):
        import runtime
        runtime.known_bots["someserver"] = {"channels": {"#music": {"files": 7, "since": 1.0}}}
        _write_zip(self.zip_path, [(f"{BOT}-2026-08-27.txt", _list_txt(base_name=BOT))])
        list_fetch.process_fetched_list_zip(BOT, self.zip_path, channel="#music")
        self.assertEqual(config.fetched_bot_lists[KEY]["lists"][""]["advert_signature"],
                         {"files": 7, "since": 1.0})

    def test_a_secondary_markers_signature_is_the_channel_it_came_from(self):
        import runtime
        _write_zip(self.zip_path, [(f"{BOT}-2026-08-27.txt", _list_txt(base_name=BOT))])
        list_fetch.process_fetched_list_zip(BOT, self.zip_path, channel="#music")
        runtime.known_bots["someserver"] = {"channels": {"#video": {"files": 9, "since": 2.0}}}
        video_zip = os.path.join(self.tmp, "v.zip")
        _write_zip(video_zip, [(f"{BOT}-VIDEO-2026-08-27.txt", _list_txt(base_name=BOT))])
        list_fetch.process_fetched_list_zip(BOT, video_zip, channel="#video", secondary=True)
        self.assertEqual(config.fetched_bot_lists[KEY]["lists"]["video"]["advert_signature"],
                         {"files": 9, "since": 2.0})


class ABotHeldBeforeChannelsWereTracked(DCCoreTestCase):
    """A real incident, hit live on the real bot: an entry fetched before
    #1232 ever existed has no "channel" field at all - sometimes not even a
    "lists" dict, only the old flat list_path/entry_count shape from before
    #1209's multi-list-per-archive feature.

    #1240 REVIEW, THE CRITICAL FINDING: an earlier version of this code
    GUESSED "secondary" from exactly this shape - no channel on record, but
    real content held - which meant EVERY ordinary refresh of such a bot
    (AUTO_REFETCH_LISTS, a manual fetch, a Downloads retry: every one of them
    passes no special flag, they are just asking the bot for its list again)
    looked like a secondary fetch forever. The channel was never backfilled,
    the content was merged beside itself instead of replaced, and the
    "arrived" count and freshness timestamp stopped moving - confirmed, with
    a full repro, on review. The fix is explicit: `secondary` is a parameter
    only list_grab.secondary_channel_tick() ever passes as True, never
    inferred here from what is or is not on record. The first test below is
    that exact regression; the rest confirm a genuinely CONFIRMED secondary
    fetch (secondary=True) still merges safely beside either legacy shape,
    same as before."""

    def setUp(self):
        super().setUp()
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dccore-1240-legacy-test-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        config.FETCHED_FILES_DIR = self.tmp
        self.video_zip = os.path.join(self.tmp, "video.zip")

    def fetch_video(self, secondary=True):
        _write_zip(self.video_zip, [(f"{BOT}-VIDEO-2026-08-27.txt",
                                     _list_txt(base_name=BOT, files=(("Clip.mkv", "700.0MB"),)))])
        return list_fetch.process_fetched_list_zip(BOT, self.video_zip, channel="#video",
                                                    secondary=secondary)

    def test_an_ordinary_refresh_of_a_legacy_bot_backfills_its_channel_instead_of_merging(self):
        """THE REGRESSION ITSELF. An ordinary refresh (secondary=False, what
        every real caller but the discovery tick ever passes) of a bot with
        no channel on record must REPLACE the entry and record the real
        channel - not merge beside it, which an earlier version did forever,
        because it read "no channel on record" as "this must be secondary"."""
        config.fetched_bot_lists[KEY] = {
            "bot": BOT, "entry_count": 1116789,
            "list_path": "/somewhere/real/list.txt",  # no "channel" key, no "lists" dict
        }
        ok, _reason = self.fetch_video(secondary=False)
        self.assertTrue(ok, _reason)
        entry = config.fetched_bot_lists[KEY]
        self.assertEqual(entry["channel"], "#video", "the channel must be backfilled, not left unknown")
        self.assertEqual(set(entry["lists"]), {""}, "an ordinary refresh replaces, it does not merge")
        self.assertEqual(entry["lists"][""]["entry_count"], 1)

    def test_a_confirmed_secondary_fetch_of_a_bot_with_no_lists_dict_at_all_keeps_its_old_content(self):
        """The pre-#1209 flat shape: the one list lives in list_path/
        entry_count directly on the entry, no "lists" dict exists yet."""
        config.fetched_bot_lists[KEY] = {
            "bot": BOT, "entry_count": 1116789,
            "list_path": "/somewhere/real/list.txt",
        }
        ok, _reason = self.fetch_video(secondary=True)
        self.assertTrue(ok, _reason)
        lists = config.fetched_bot_lists[KEY]["lists"]
        self.assertEqual(lists[""]["entry_count"], 1116789)
        self.assertEqual(lists[""]["list_path"], "/somewhere/real/list.txt")
        self.assertIn("video", lists)
        self.assertEqual(lists["video"]["entry_count"], 1)

    def test_a_confirmed_secondary_fetch_of_a_bot_with_a_lists_dict_but_no_channel_field_keeps_its_markers(self):
        """The #1209-but-pre-#1232 shape: a real "lists" dict (maybe Main
        and RAR already), just never tagged with which channel it came
        from."""
        config.fetched_bot_lists[KEY] = {
            "bot": BOT,
            "lists": {
                "": {"list_path": "/x/main.txt", "entry_count": 500, "file_name": "main.txt"},
                "RAR": {"list_path": "/x/rar.txt", "entry_count": 12, "file_name": "rar.txt"},
            },
        }
        ok, _reason = self.fetch_video(secondary=True)
        self.assertTrue(ok, _reason)
        lists = config.fetched_bot_lists[KEY]["lists"]
        self.assertEqual(lists[""]["entry_count"], 500)
        self.assertEqual(lists["RAR"]["entry_count"], 12)
        self.assertIn("video", lists)

    def test_an_ordinary_fetch_with_nothing_held_at_all_takes_the_full_replace_path(self):
        """No previous entry whatsoever - the FIRST ever fetch for a bot
        must still take the ordinary, full-replace path."""
        ok, _reason = self.fetch_video(secondary=False)
        self.assertTrue(ok, _reason)
        self.assertEqual(set(config.fetched_bot_lists[KEY]["lists"]), {""})


class TheMergeHelpersInIsolation(DCCoreTestCase):

    def test_channel_marker_name_strips_the_hash(self):
        self.assertEqual(list_fetch._channel_marker_name("#video"), "video")

    def test_channel_marker_name_sanitises_unsafe_characters(self):
        self.assertEqual(list_fetch._channel_marker_name("#a/b"), "a_b")

    def test_channel_marker_name_never_returns_the_empty_marker(self):
        """"" means "the main list" - a channel name must never collide with
        that meaning by sanitising down to nothing."""
        self.assertEqual(list_fetch._channel_marker_name("#"), "channel")
        self.assertEqual(list_fetch._channel_marker_name(""), "channel")

    def test_extract_dir_for_is_the_ordinary_one_by_default(self):
        self.assertEqual(list_fetch._extract_dir_for(BOT, "#video"),
                         list_fetch.list_extract_dir(BOT))

    def test_extract_dir_for_is_the_secondary_one_when_told_so(self):
        self.assertEqual(list_fetch._extract_dir_for(BOT, "#video", secondary=True),
                         list_fetch.secondary_channel_extract_dir(BOT, "#video"))

    def test_secondary_extract_dir_is_not_nested_under_the_primarys_own(self):
        """#1240 review, confirmed bug: an earlier version put this INSIDE
        the primary's own directory, which _hold_existing_list()/_release_
        held_list() rename and rmtree WHOLE on every ordinary refresh of the
        primary - deleting a secondary channel's files outright, even though
        its marker and index rows (held in config.fetched_bot_lists, not on
        this path) survived untouched. Must be a sibling, never nested, so
        nothing that ever touches the primary's directory can reach it."""
        primary = os.path.normpath(list_fetch.list_extract_dir(BOT))
        secondary = os.path.normpath(list_fetch.secondary_channel_extract_dir(BOT, "#video"))
        self.assertFalse(secondary == primary or secondary.startswith(primary + os.sep),
                         f"{secondary!r} must not be inside {primary!r}")

    def test_disambiguate_marker_leaves_an_uncontested_name_alone(self):
        self.assertEqual(list_fetch._disambiguate_marker("video", "#video", set()), "video")

    def test_disambiguate_marker_is_case_insensitive(self):
        """"#RAR" sanitises to the same name a filename-derived "RAR" marker
        already uses - a plain "not already a key" check would miss this,
        since "RAR" (the reserved name) and "RAR" (the candidate) really are
        the same string here, but the general case ("#rar" vs "#RAR", which
        differ only by case) needs the same fold."""
        name = list_fetch._disambiguate_marker("RAR", "#RAR", {"rar"})
        self.assertNotEqual(name, "RAR")
        self.assertTrue(name.startswith("RAR-"), name)

    def test_disambiguate_marker_is_stable_across_repeated_calls(self):
        """The SAME channel must land on the SAME disambiguated name every
        time it is fetched - a counter would depend on fetch order, which
        would rename a marker (and the files/index rows a reader is holding
        under its old name) out from under whatever already pointed at it."""
        first = list_fetch._disambiguate_marker("a_b", "#a|b", {"a_b"})
        second = list_fetch._disambiguate_marker("a_b", "#a|b", {"a_b"})
        self.assertEqual(first, second)

    def test_disambiguate_marker_differs_for_two_channels_that_collide_with_each_other(self):
        """"#a|b" and "#a_b" both sanitise to "a_b" - hashing the collided
        NAME (identical for both, by definition of a collision) could never
        tell them apart; hashing the CHANNEL itself does."""
        one = list_fetch._disambiguate_marker("a_b", "#a|b", {"a_b"})
        two = list_fetch._disambiguate_marker("a_b", "#a_b", {"a_b"})
        self.assertNotEqual(one, two)


if __name__ == "__main__":
    unittest.main()
