"""#926 item 5: the list of a bot that advertises one and whose list is not
held is grabbed automatically - opt-in, and on AutoGet's rules.

One grab at a time and at most one every AUTO_GRAB_EVERY_MINUTES; a random
5-360 second wait first; dropped if someone else asks that bot meanwhile; three
tries 30 minutes apart, then it stops; bots below the minimum files or speed,
and "servers only" bots, are skipped; a list removed by hand is not grabbed
back.
"""

import io
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import db  # noqa: E402
import defaults as config  # noqa: E402
import list_grab  # noqa: E402
import runtime  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

NOW = 1_000_000.0


class GrabCase(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(AUTO_GRAB_LISTS=True, AUTO_GRAB_EVERY_MINUTES=10, AUTO_GRAB_MIN_FILES=0,
                        AUTO_GRAB_MIN_SPEED_KB=0, bot_joined_channel=True, NICKNAME="OurBot")
        config.channel_users["#chan"] = set()
        self.asked = []
        self.answer = (200, {})
        real = webserver.build_list_fetch_enqueue_result
        webserver.build_list_fetch_enqueue_result = lambda bot: (self.asked.append(bot), self.answer)[1]
        self.addCleanup(setattr, webserver, "build_list_fetch_enqueue_result", real)
        self.lines = []

    def advertise(self, nick, online=True, **fields):
        entry = dict({"nick": nick, "files": 5000, "last_seen": NOW}, **fields)
        runtime.known_bots[nick.lower()] = entry
        if online:
            config.channel_users["#chan"].add(nick.lower())

    def tick(self, now, delay=30.0):
        return list_grab.tick(now=now, log=self.lines.append, pick_delay=lambda: delay)

    def grab(self, start=NOW, delay=30.0):
        """Plan, then run the wait out. Returns the second tick's answer."""
        self.assertEqual(self.tick(start, delay), "planned")
        return self.tick(start + delay, delay)


class TheRules(GrabCase):
    def test_a_bot_with_no_list_held_is_asked_after_the_wait(self):
        self.advertise("PackBot")
        self.assertEqual(self.tick(NOW), "planned")
        self.assertEqual(self.tick(NOW + 29), "waiting")
        self.assertEqual(self.asked, [])
        self.assertEqual(self.tick(NOW + 30), "asked")
        self.assertEqual(self.asked, ["PackBot"])

    def test_the_wait_is_random_between_5_and_360_seconds(self):
        self.assertEqual(list_grab.GRAB_DELAY_SECONDS, (5.0, 360.0))
        self.advertise("PackBot")
        list_grab.tick(now=NOW, log=self.lines.append)
        wait = runtime.list_grab_plan["at"] - NOW
        self.assertTrue(5.0 <= wait <= 360.0, wait)

    def test_off_is_off(self):
        self.set_config(AUTO_GRAB_LISTS=False)
        self.advertise("PackBot")
        self.assertEqual(self.tick(NOW), "off")

    def test_not_before_the_channels_are_joined(self):
        self.set_config(bot_joined_channel=False)
        self.advertise("PackBot")
        self.assertEqual(self.tick(NOW), "off")

    def test_one_grab_every_n_minutes(self):
        self.advertise("PackBot")
        self.advertise("LoadBot")
        self.assertEqual(self.grab(), "asked")
        self.assertEqual(self.tick(NOW + 30 + 9 * 60), "waiting")
        self.assertEqual(self.grab(NOW + 30 + 10 * 60), "asked")
        self.assertEqual(sorted(self.asked), ["LoadBot", "PackBot"])

    def test_the_biggest_list_first(self):
        self.advertise("SmallBot", files=100)
        self.advertise("BigBot", files=90000)
        self.grab()
        self.assertEqual(self.asked, ["BigBot"])

    def test_someone_else_asking_during_the_wait_drops_ours(self):
        self.advertise("PackBot")
        self.assertEqual(self.tick(NOW), "planned")
        list_grab.note_someone_else_asked("SomeUser", "@PackBot", now=NOW + 10)
        self.assertEqual(self.tick(NOW + 12), "someone else asked")
        self.assertEqual(self.asked, [])
        self.assertEqual(self.tick(NOW + 60), "nothing", "left alone for a while")
        self.assertEqual(self.grab(NOW + 10 + list_grab.OTHERS_ASKED_SECONDS), "asked")

    def test_what_counts_as_someone_else_asking(self):
        self.advertise("PackBot")
        list_grab.note_someone_else_asked("SomeUser", "@PackBot find this", now=NOW)
        list_grab.note_someone_else_asked("PackBot", "@PackBot", now=NOW)
        list_grab.note_someone_else_asked("SomeUser", "@NotABot", now=NOW)
        self.assertEqual(runtime.list_grab_others_asked, {})
        list_grab.note_someone_else_asked("SomeUser", "  @packbot ", now=NOW)
        self.assertEqual(runtime.list_grab_others_asked, {"packbot": NOW})

    def test_three_tries_thirty_minutes_apart_then_it_stops(self):
        self.set_config(AUTO_GRAB_EVERY_MINUTES=0)
        self.advertise("PackBot")
        at = NOW
        self.assertEqual(self.grab(at), "asked")
        self.assertEqual(self.tick(at + 30 + 29 * 60), "nothing", "cooling down")
        at += 30 + 30 * 60
        self.assertEqual(self.grab(at), "asked")
        at += 30 + 30 * 60
        self.assertEqual(self.grab(at), "asked")
        self.assertEqual(self.tick(at + 10 * 3600), "nothing", "gave up after three")
        self.assertEqual(self.asked, ["PackBot"] * 3)
        self.assertTrue(any("try 3 of 3" in line for line in self.lines))

    def test_the_tries_survive_a_restart(self):
        self.set_config(AUTO_GRAB_EVERY_MINUTES=0)
        self.advertise("PackBot")
        self.grab()
        with io.open(db.LIST_GRABS_FILE, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["tries"]["packbot"]["tries"], 1)
        runtime.list_grab_state = None
        self.assertEqual(list_grab._state()["tries"]["packbot"]["tries"], 1)


class WhoIsSkipped(GrabCase):
    def reason(self, nick, now=NOW):
        entry = runtime.known_bots[nick.lower()]
        return list_grab._why_not(nick.lower(), entry, webserver.present_nicks(), now)

    def test_a_held_list(self):
        self.advertise("PackBot")
        self.set_config(fetched_bot_lists={"packbot": {"bot": "PackBot", "fetched_at": 1.0}})
        self.assertEqual(self.reason("PackBot"), "held")

    def test_offline(self):
        self.advertise("PackBot", online=False)
        config.channel_users["#chan"].add("someoneelse")
        self.assertEqual(self.reason("PackBot"), "offline")

    def test_ourselves(self):
        self.advertise("OurBot")
        self.assertEqual(self.reason("OurBot"), "us")

    def test_a_bot_that_advertises_no_list(self):
        self.advertise("RarBot", files=None)
        self.assertEqual(self.reason("RarBot"), "no list advertised")

    def test_too_few_files(self):
        self.set_config(AUTO_GRAB_MIN_FILES=1000)
        self.advertise("SmallBot", files=999)
        self.advertise("BigBot", files=1000)
        self.assertEqual(self.reason("SmallBot"), "too few files")
        self.assertIsNone(self.reason("BigBot"))

    def test_servers_only(self):
        self.advertise("PackBot", mode="Servers Only")
        self.assertEqual(self.reason("PackBot"), "servers only")
        self.advertise("OpenBot", mode="Normal")
        self.assertIsNone(self.reason("OpenBot"))

    def test_too_slow_only_when_it_says(self):
        self.set_config(AUTO_GRAB_MIN_SPEED_KB=50)
        self.advertise("SlowBot", speed="45000cps")
        self.advertise("FastBot", speed="120KB/s")
        self.advertise("QuietBot")
        self.assertEqual(self.reason("SlowBot"), "too slow")
        self.assertIsNone(self.reason("FastBot"))
        self.assertIsNone(self.reason("QuietBot"))

    def test_a_list_removed_by_hand_is_not_grabbed_back(self):
        self.advertise("PackBot")
        list_grab.note_removed_by_hand("PackBot")
        self.assertEqual(self.reason("PackBot"), "removed by hand")
        runtime.list_grab_state = None
        self.assertEqual(self.reason("PackBot"), "removed by hand", "kept on disk")

    def test_a_bot_that_left_during_the_wait_is_not_asked(self):
        self.advertise("PackBot")
        self.assertEqual(self.tick(NOW), "planned")
        config.channel_users["#chan"] = {"someoneelse"}
        self.assertEqual(self.tick(NOW + 30), "skipped: offline")
        self.assertEqual(self.asked, [])


class Speeds(unittest.TestCase):
    def test_what_adverts_say(self):
        self.assertEqual(list_grab.advertised_speed("45000cps"), 45000)
        self.assertEqual(list_grab.advertised_speed("45,000cps"), 45000)
        self.assertEqual(list_grab.advertised_speed("120KB/s"), 120 * 1024)
        self.assertEqual(list_grab.advertised_speed("1.5MB/s"), 1.5 * 1024 ** 2)
        self.assertEqual(list_grab.advertised_speed("4500"), 4500)
        self.assertIsNone(list_grab.advertised_speed("fast"))
        self.assertIsNone(list_grab.advertised_speed(None))


class TheWiring(GrabCase):
    def read(self, name):
        with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as handle:
            return handle.read()

    def test_the_worker_starts_once_and_only_when_on(self):
        started = []
        self.set_config(AUTO_GRAB_LISTS=False)
        self.assertFalse(list_grab.ensure_worker(start=lambda: started.append(1)))
        self.set_config(AUTO_GRAB_LISTS=True)
        self.assertTrue(list_grab.ensure_worker(start=lambda: started.append(1)))
        self.assertFalse(list_grab.ensure_worker(start=lambda: started.append(1)))
        self.assertEqual(started, [1])

    def test_the_loop_survives_a_tick_that_raises(self):
        real = list_grab.tick

        def explode(*_a, **_k):
            raise RuntimeError("boom")

        list_grab.tick = explode
        self.addCleanup(setattr, list_grab, "tick", real)

        class Enough(Exception):
            pass

        def stop(_seconds):
            raise Enough()

        with self.assertRaises(Enough):
            list_grab.worker(sleep=stop)

    def test_boot_and_rehash_start_it(self):
        self.assertIn("list_grab.ensure_worker()", self.read("oserve.py"))
        self.assertIn("if _list_grab.ensure_worker():", self.read("commands.py"))

    def test_irc_reports_every_channel_ask(self):
        code = self.read("irc.py")
        at = code.index("_capture_channel_advert(user, target_chan, msg)\n")
        self.assertIn("_capture_list_ask(user, msg)", code[at:at + 400])

    def test_the_irc_capture_passes_it_on(self):
        import irc
        self.advertise("PackBot")
        irc._capture_list_ask("SomeUser", "@PackBot")
        self.assertIn("packbot", runtime.list_grab_others_asked)

    def test_purging_one_list_marks_it_removed(self):
        code = self.read("list_fetch.py")
        at = code.index("def purge_fetched_list(source):")
        self.assertIn("list_grab.note_removed_by_hand(nick)", code[at:])


if __name__ == "__main__":
    unittest.main()
