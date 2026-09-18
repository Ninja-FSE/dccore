"""#376, part 2: a bot that never advertises is invisible.

The List Browser's sidebar is built from adverts we have seen
(runtime.known_bots). A bot that answers "@nick" perfectly well but does not
advertise on a channel we are in had no row, so there was no way to fetch
its list from the dashboard. Confirmed absent before this: webserver.py had
/api/filelists, /fetch, /fetch-folder-rar, /bots, /search and /bot/<nick>,
and nothing that adds a source.

Now the operator names one. It goes into the SAME registry, flagged
hand_entered, so every reader of the sidebar works on it unchanged - and it
survives the registry's pruning, which otherwise forgets a bot a week after
its last advert (a hand-entered one never advertises).
"""

import io
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_webserver import log_in_test_client, WEBUI_TEST_PASSWORD  # noqa: E402

T0 = 1_000_000.0
WEEK = 7 * 24 * 60 * 60


class _RegistryCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        runtime.known_bots.clear()
        self.addCleanup(runtime.known_bots.clear)
        config.fetched_bot_lists.clear()
        self.flushes = []
        self._real_flush = irc._flush_known_bots
        irc._flush_known_bots = lambda now=None, force=False: self.flushes.append(force) or True
        self.addCleanup(setattr, irc, "_flush_known_bots", self._real_flush)
        self.set_config(NICKNAME="OurBot")


class AddingOne(_RegistryCase):
    def test_a_nick_becomes_a_registry_entry_flagged_by_hand(self):
        status, result = webserver.build_add_source_result("QuietBot")

        self.assertEqual(status, 200)
        self.assertEqual(result, {"added": "QuietBot", "already_known": False})
        entry = runtime.known_bots["quietbot"]
        self.assertEqual(entry["nick"], "QuietBot")
        self.assertIs(entry["hand_entered"], True)
        self.assertEqual(entry["last_seen"], 0, "no advert has been seen; none is claimed")

    def test_it_is_persisted_at_once(self):
        """The registry is otherwise flushed on a 30 s timer; an operator's
        own action must not be lost to a restart inside that window."""
        webserver.build_add_source_result("QuietBot")

        self.assertEqual(self.flushes, [True])

    def test_a_bot_already_seen_advertising_is_kept_not_duplicated(self):
        runtime.known_bots["loudbot"] = {"nick": "LoudBot", "last_seen": T0, "files": 500}

        status, result = webserver.build_add_source_result("loudbot")

        self.assertEqual(status, 200)
        self.assertEqual(result["already_known"], True)
        entry = runtime.known_bots["loudbot"]
        self.assertEqual(entry["files"], 500, "the advert's own fields survive")
        self.assertEqual(entry["nick"], "LoudBot", "the advertised spelling wins")
        self.assertIs(entry["hand_entered"], True)

    def test_whitespace_is_trimmed(self):
        webserver.build_add_source_result("  QuietBot  ")
        self.assertIn("quietbot", runtime.known_bots)

    def test_a_blank_nick_is_refused(self):
        for blank in ("", "   ", None):
            with self.subTest(nick=blank):
                status, result = webserver.build_add_source_result(blank)
                self.assertEqual(status, 400)
                self.assertIn("error", result)
        self.assertEqual(runtime.known_bots, {})

    def test_what_is_not_a_nick_is_refused(self):
        """It becomes a registry key and a PRIVMSG target: a channel, a
        sentence, a nick with a space or a control character cannot."""
        for bad in ("#channel", "two words", "1starts-with-digit", "nick\x01",
                    "nick\r\nPRIVMSG", "x" * 65):
            with self.subTest(nick=bad):
                status, _result = webserver.build_add_source_result(bad)
                self.assertEqual(status, 400)
        self.assertEqual(runtime.known_bots, {})

    def test_our_own_nick_is_refused(self):
        status, result = webserver.build_add_source_result("ourbot")
        self.assertEqual(status, 400)
        self.assertIn("own nick", result["error"])

    def test_nick_punctuation_is_allowed(self):
        for ok in ("Bot[1]", "Bot`", "Bot^", "Bot-2", "Bot_x", "{Bot}"):
            with self.subTest(nick=ok):
                status, _ = webserver.build_add_source_result(ok)
                self.assertEqual(status, 200)


class ItShowsUpAsARow(_RegistryCase):
    def rows(self):
        return {r["bot"]: r for r in webserver.build_fetched_bot_list_summaries()}

    def test_a_hand_entered_bot_is_a_not_held_row_tagged_by_hand(self):
        webserver.build_add_source_result("QuietBot")

        row = self.rows()["QuietBot"]
        self.assertFalse(row["held"])
        self.assertIs(row["hand_entered"], True)
        self.assertEqual(row["freshness"], "not_held")
        self.assertIsNone(row["count"], "no advert, no count - not zero")

    def test_an_advert_only_row_is_not_tagged(self):
        runtime.known_bots["loudbot"] = {"nick": "LoudBot", "last_seen": T0, "files": 500}

        row = self.rows()["LoudBot"]
        self.assertIs(row["hand_entered"], False)

    def test_presence_is_not_claimed(self):
        """Grey, not red: nobody has looked."""
        config.channel_users.clear()
        webserver.build_add_source_result("QuietBot")

        self.assertIsNone(self.rows()["QuietBot"]["online"])

    def test_an_advert_later_fills_the_row_in(self):
        """irc._record_bot() merges into the same entry - the count and the
        freshness fields arrive, and the flag survives."""
        webserver.build_add_source_result("QuietBot")

        irc._record_bot("quietbot", "QuietBot", "#chan",
                        {"family": "omenserve", "files": 1234, "list_date": "2026-09-18"}, T0)

        entry = runtime.known_bots["quietbot"]
        self.assertIs(entry["hand_entered"], True)
        self.assertEqual(self.rows()["QuietBot"]["count"], 1234)


class ItSurvivesPruning(_RegistryCase):
    """An advert-only entry is forgotten a week after its last advert, or a
    day after it is confirmed absent. A hand-entered one has no adverts to
    age on, so age says nothing about it."""

    def test_the_week_ttl_does_not_apply(self):
        webserver.build_add_source_result("QuietBot")
        runtime.known_bots["oldbot"] = {"nick": "OldBot", "last_seen": T0 - 2 * WEEK}

        irc._prune_known_bots(T0)

        self.assertIn("quietbot", runtime.known_bots)
        self.assertNotIn("oldbot", runtime.known_bots, "the ordinary rule still applies to others")

    def test_confirmed_absent_does_not_apply_either(self):
        config.channel_users["#chan"] = {"someone_else"}
        webserver.build_add_source_result("QuietBot")

        irc._prune_known_bots(T0 + 3 * 24 * 3600)

        self.assertIn("quietbot", runtime.known_bots)

    def test_the_size_cap_evicts_others_first(self):
        """With last_seen 0 it would otherwise sort as the oldest of all
        and be the first to go."""
        self.addCleanup(setattr, irc, "KNOWN_BOTS_MAX", irc.KNOWN_BOTS_MAX)
        irc.KNOWN_BOTS_MAX = 2
        webserver.build_add_source_result("QuietBot")
        for i, nick in enumerate(("a", "b", "c")):
            runtime.known_bots[nick] = {"nick": nick, "last_seen": T0 - 10 + i}

        irc._prune_known_bots(T0)

        self.assertIn("quietbot", runtime.known_bots)
        self.assertNotIn("a", runtime.known_bots)


class ForgettingOne(_RegistryCase):
    def test_a_hand_entered_bot_is_removed_and_persisted(self):
        webserver.build_add_source_result("QuietBot")
        self.flushes.clear()

        status, result = webserver.build_remove_source_result("quietbot")

        self.assertEqual(status, 200)
        self.assertEqual(result, {"removed": "quietbot"})
        self.assertNotIn("quietbot", runtime.known_bots)
        self.assertEqual(self.flushes, [True])

    def test_an_unknown_nick_is_404(self):
        status, _ = webserver.build_remove_source_result("nobody")
        self.assertEqual(status, 404)

    def test_a_bot_that_advertises_cannot_be_forgotten_here(self):
        """It would be back at its next advert."""
        runtime.known_bots["loudbot"] = {"nick": "LoudBot", "last_seen": T0}

        status, result = webserver.build_remove_source_result("LoudBot")

        self.assertEqual(status, 409)
        self.assertIn("advertises", result["error"])
        self.assertIn("loudbot", runtime.known_bots)

    def test_a_held_list_has_to_be_purged_first(self):
        webserver.build_add_source_result("QuietBot")
        config.fetched_bot_lists["quietbot"] = {"bot": "QuietBot", "fetched_at": T0}

        status, result = webserver.build_remove_source_result("QuietBot")

        self.assertEqual(status, 409)
        self.assertIn("purge", result["error"])
        self.assertIn("quietbot", runtime.known_bots)

    def test_unsafe_input_is_refused_before_anything_is_looked_up(self):
        status, _ = webserver.build_remove_source_result("nick\r\nQUIT")
        self.assertEqual(status, 400)


class OverHttp(_RegistryCase):
    def setUp(self):
        super().setUp()
        self.set_config(ADMIN_PASSWORD_HASH=adminchat.make_password_hash(
            WEBUI_TEST_PASSWORD, iterations=1000))
        self.client = webserver.create_app().test_client()
        log_in_test_client(self.client)

    def test_add_then_it_is_in_the_bots_payload_then_forget(self):
        resp = self.client.post("/api/filelists/sources", json={"bot": "QuietBot"})
        self.assertEqual(resp.status_code, 200, resp.get_json())

        bots = self.client.get("/api/filelists/bots").get_json()
        row = next(r for r in bots if r["bot"] == "QuietBot")
        self.assertTrue(row["hand_entered"])

        resp = self.client.post("/api/filelists/sources/QuietBot/remove")
        self.assertEqual(resp.status_code, 200, resp.get_json())
        bots = self.client.get("/api/filelists/bots").get_json()
        self.assertEqual([r for r in bots if r["bot"] == "QuietBot"], [])

    def test_a_body_that_is_not_an_object_is_a_400_not_a_500(self):
        for bad in (["a"], "text", 7):
            with self.subTest(body=bad):
                resp = self.client.post("/api/filelists/sources", json=bad)
                self.assertEqual(resp.status_code, 400)


class ThePageOffersIt(unittest.TestCase):
    def read(self, *parts):
        with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
            return handle.read()

    def test_the_form_exists_with_add_and_forget(self):
        html = self.read("web", "index.html")
        self.assertIn('id="filelists-add-source-form"', html)
        self.assertIn('id="filelists-add-source-input"', html)
        self.assertIn('id="filelists-forget-source-btn"', html)

    def test_the_page_posts_to_the_two_routes(self):
        js = self.read("web", "app.js")
        self.assertIn('postJson("/api/filelists/sources", { bot: nick })', js)
        self.assertIn('"/api/filelists/sources/" + encodeURIComponent(nick) + "/remove"', js)

    def test_the_row_is_tagged_only_when_hand_entered_and_not_held(self):
        js = self.read("web", "app.js")
        self.assertIn("if (primary.hand_entered && !primary.held) {", js)
        self.assertIn('t("filelists.handEnteredTag")', js)

    def test_every_new_key_is_in_all_three_dictionaries(self):
        keys = ("filelists.addSourceLabel", "filelists.addSource", "filelists.forgetSource",
                "filelists.sourceAdded", "filelists.sourceAlreadyKnown", "filelists.sourceForgotten",
                "filelists.couldNotAddSource", "filelists.couldNotForgetSource",
                "filelists.typeANickToForget", "filelists.handEnteredTag", "filelists.handEnteredTitle")
        for lang in ("en", "fr", "es"):
            d = json.loads(self.read("web", "lang", lang + ".json"))
            for key in keys:
                self.assertIn(key, d, lang)


if __name__ == "__main__":
    unittest.main()
