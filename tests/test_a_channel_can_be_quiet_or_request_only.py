"""A channel a list serves can be Normal, Quiet or Request only (#1204).

An operator who wants a channel served without the bot announcing itself in it
had no setting for that. The mode is stored per channel on the list that
serves it, and an old lists.json - which has no mode anywhere - has to read as
Normal everywhere, or every existing install would change behaviour on upgrade.

  normal        advert, answers, notices - as it always was
  quiet         no advert; still answers and still sends its notices
  request_only  no advert and no notices; it serves silently
"""

import io
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import announce  # noqa: E402
import commands  # noqa: E402
import defaults as config  # noqa: E402
import library  # noqa: E402
import webserver  # noqa: E402
from tests.support import DCCoreTestCase, bots_in_the_channel  # noqa: E402


class ModeCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.store = os.path.join(self.tree.root, "lists.json")
        self.set_config(LISTS_FILE=self.store,
                        LIBRARY_FOLDERS_FILE=os.path.join(self.tree.root, "library_folders.json"),
                        FILE_DIRECTORY=self.tree.music)

    def write(self, entries):
        with io.open(self.store, "w", encoding="utf-8") as handle:
            json.dump(entries, handle)

    def entry(self, **over):
        row = {"name": "Main", "primary": True, "channels": ["#a", "#b", "#c"],
               "folders": [{"name": "Music", "path": self.tree.music}]}
        row.update(over)
        return row

    def told(self):
        return "".join(m for _u, m, *_ in self.oserve.queued)


class AnOldFileReadsAsNormalEverywhere(ModeCase):
    def test_no_modes_key_is_normal(self):
        self.write([self.entry()])
        for channel in ("#a", "#b", "#c", "#nowhere"):
            self.assertEqual(library.channel_mode(channel), library.NORMAL)

    def test_a_list_built_without_modes_is_normal(self):
        self.assertEqual(library.ServedList("x", True, ["#a"], []).modes, {})

    def test_no_file_at_all_is_normal(self):
        self.assertEqual(library.channel_mode("#a"), library.NORMAL)

    def test_an_unknown_mode_in_the_file_reads_as_normal(self):
        self.write([self.entry(modes={"#a": "loud", "#b": "quiet"})])
        self.assertEqual(library.channel_mode("#a"), library.NORMAL)
        self.assertEqual(library.channel_mode("#b"), library.QUIET)

    def test_a_mode_on_a_channel_the_list_does_not_serve_is_dropped(self):
        self.write([self.entry(modes={"#elsewhere": "quiet"})])
        self.assertEqual(library.lists()[0].modes, {})


class AModeIsStoredPerChannel(ModeCase):
    def test_it_round_trips_and_is_case_insensitive(self):
        self.write([self.entry(modes={"#A": "quiet", "#b": "request_only"})])
        self.assertEqual(library.channel_mode("#a"), library.QUIET)
        self.assertEqual(library.channel_mode("#B"), library.REQUEST_ONLY)
        self.assertEqual(library.channel_mode("#c"), library.NORMAL)

    def test_save_keeps_only_what_is_not_normal(self):
        folders = [library.Folder("Music", self.tree.music)]
        library.save_lists([library.ServedList(
            "Main", True, ["#a", "#b"], folders, {"#a": "quiet", "#b": "normal"})])
        with io.open(self.store, encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertEqual(stored[0]["modes"], {"#a": "quiet"})

    def test_save_writes_no_modes_key_when_there_are_none(self):
        folders = [library.Folder("Music", self.tree.music)]
        library.save_lists([library.ServedList("Main", True, ["#a"], folders)])
        with io.open(self.store, encoding="utf-8") as handle:
            self.assertNotIn("modes", json.load(handle)[0])

    def test_save_refuses_a_mode_it_does_not_know(self):
        folders = [library.Folder("Music", self.tree.music)]
        with self.assertRaises(ValueError):
            library.save_lists([library.ServedList(
                "Main", True, ["#a"], folders, {"#a": "loud"})])

    def test_save_refuses_a_mode_on_a_channel_not_served(self):
        folders = [library.Folder("Music", self.tree.music)]
        with self.assertRaises(ValueError):
            library.save_lists([library.ServedList(
                "Main", True, ["#a"], folders, {"#z": "quiet"})])

    def test_two_lists_keep_their_own_modes(self):
        self.write([
            self.entry(channels=["#a"], modes={"#a": "quiet"}),
            self.entry(name="Other", primary=False, channels=["#b"]),
        ])
        self.assertEqual(library.channel_mode("#a"), library.QUIET)
        self.assertEqual(library.channel_mode("#b"), library.NORMAL)


class APrivateMessageTakesTheModeOfASharedChannel(ModeCase):
    def setUp(self):
        super().setUp()
        self.write([self.entry(channels=["#a", "#b"],
                               modes={"#a": "request_only", "#b": "quiet"})])

    def test_a_channel_request_uses_that_channel(self):
        self.assertEqual(library.mode_for_request("#a", "dave"), library.REQUEST_ONLY)
        self.assertEqual(library.mode_for_request("#b", "dave"), library.QUIET)

    def test_a_pm_uses_the_first_shared_bound_channel(self):
        bots_in_the_channel("dave", channel="#b")
        self.assertEqual(library.mode_for_request("dave", "dave"), library.QUIET)

    def test_a_pm_from_someone_in_no_bound_channel_is_normal(self):
        bots_in_the_channel("dave", channel="#unbound")
        self.assertEqual(library.mode_for_request("dave", "dave"), library.NORMAL)

    def test_a_pm_from_a_stranger_is_normal(self):
        self.assertEqual(library.mode_for_request("erin", "erin"), library.NORMAL)

    def test_a_status_prefix_on_the_member_does_not_hide_them(self):
        config.channel_users.setdefault("#a", set()).add("@dave")
        self.assertEqual(library.mode_for_request("dave", "dave"), library.REQUEST_ONLY)


class NoticesFollowTheMode(ModeCase):
    def setUp(self):
        super().setUp()
        self.write([self.entry(channels=["#n", "#q", "#r"],
                               modes={"#q": "quiet", "#r": "request_only"})])

    def test_normal_and_quiet_send_the_error(self):
        for channel in ("#n", "#q"):
            self.oserve.queued.clear()
            announce.send_dcc_error("dave", "file_not_found", channel=channel)
            self.assertTrue(self.told(), channel)

    def test_request_only_sends_nothing(self):
        announce.send_dcc_error("dave", "file_not_found", channel="#r")
        announce.send_dcc_queue_notice("dave", "Song.flac", 2, channel="#r")
        announce.send_dcc_already_queued_notice("dave", "Song.flac", 2, channel="#r")
        announce.send_pack_error_notice(None, "dave", channel="#r")
        self.assertEqual(self.told(), "")

    def test_quiet_still_sends_the_queue_notice(self):
        announce.send_dcc_queue_notice("dave", "Song.flac", 2, channel="#q")
        self.assertIn("Song.flac", self.told())

    def test_a_call_with_no_channel_is_unchanged(self):
        announce.send_dcc_error("dave", "file_not_found")
        self.assertTrue(self.told())

    def test_the_channel_announcement_is_for_normal_only(self):
        self.assertTrue(announce._channel_may_be_told("#n", "dave"))
        self.assertFalse(announce._channel_may_be_told("#q", "dave"))
        self.assertFalse(announce._channel_may_be_told("#r", "dave"))


class TheCommandRepliesFollowTheMode(ModeCase):
    def setUp(self):
        super().setUp()
        self.write([self.entry(channels=["#n", "#r"], modes={"#r": "request_only"})])

    def test_request_only_drops_the_replies(self):
        for handler in (commands.handle_queue_check, commands.handle_help_request,
                        commands.handle_stats_request, commands.handle_top_request):
            handler(None, "dave", "#r")
        self.assertEqual(self.told(), "")

    def test_normal_still_answers(self):
        commands.handle_help_request(None, "dave", "#n")
        self.assertTrue(self.told())

    def test_the_remove_still_removes_but_says_nothing(self):
        key = "dave"
        config.dcc_queue[key] = [{"file": "Song.flac", "path": "x"}]
        commands.handle_queue_remove(None, "dave", "#r")
        self.assertNotIn(key, config.dcc_queue)
        self.assertEqual(self.told(), "")


class TheDashboardCarriesTheModes(ModeCase):
    def post(self, **over):
        row = {"name": "Main", "primary": True, "channels": ["#a", "#b"],
               "folders": [{"name": "Music", "path": self.tree.music}]}
        row.update(over)
        return webserver.apply_list_changes({"lists": [row]})

    def test_a_saved_mode_comes_back_in_the_payload(self):
        status, _ = self.post(modes={"#a": "quiet"})
        self.assertEqual(status, 200)
        self.assertEqual(webserver.build_lists_payload()["lists"][0]["modes"], {"#a": "quiet"})

    def test_normal_is_not_stored(self):
        self.post(modes={"#a": "normal"})
        self.assertEqual(webserver.build_lists_payload()["lists"][0]["modes"], {})

    def test_a_missing_modes_is_fine(self):
        status, _ = self.post()
        self.assertEqual(status, 200)

    def test_modes_that_is_not_an_object_is_refused(self):
        status, result = self.post(modes=["#a"])
        self.assertEqual(status, 400)
        self.assertIn("modes", result["error"])

    def test_an_unknown_mode_is_refused(self):
        status, _ = self.post(modes={"#a": "loud"})
        self.assertEqual(status, 400)

    def test_a_mode_on_an_unticked_channel_is_refused(self):
        status, _ = self.post(modes={"#z": "quiet"})
        self.assertEqual(status, 400)


class TheEditorOffersTheModes(unittest.TestCase):
    def test_the_modes_are_in_every_language_and_in_the_editor(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
            source = handle.read()
        for value in ("normal", "quiet", "request_only"):
            self.assertIn('<option value="%s">' % value, source)
        for lang in ("en", "fr", "es"):
            with io.open(os.path.join(REPO_ROOT, "web", "lang", lang + ".json"),
                         encoding="utf-8") as handle:
                words = json.load(handle)
            for key in ("channelModeTitle", "channelModeNormal",
                        "channelModeQuiet", "channelModeRequestOnly"):
                self.assertIn("settings." + key, words)


if __name__ == "__main__":
    unittest.main()
