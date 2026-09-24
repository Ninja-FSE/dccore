"""#926 item 8: a bot's advertised slots, queue, speed and mode are kept and
shown - AutoGet's "slots" page, as one line under the bot in the List Browser.

The advert already said "Slots: 3/10 <> Queued: 12 <> Speed: 45000cps <> Mode:
Normal"; DCCore read the file count and date out of it and dropped the rest.
Only figures the bot actually published are kept, only an ONLINE bot's are
shown (figures from a bot that left describe a moment that is over), and none
of them take part in freshness - they change with every send.
"""

import io
import os
import sys
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import irc  # noqa: E402
import list_fetch  # noqa: E402
import runtime  # noqa: E402
import defaults as config  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

OMEN = ("Type: @PackBot For My List Of: 719,041 Files <> Slots: 3/10 <> Queued: 12 <> "
        "Speed: 45000cps <> Served: 3,456,016 <> List: Aug 10th <> Mode: Servers Only")
SPQR = ("For My List(19527files:163812MB) and DCC Status, type @LoadBot and @LoadBot-stats. "
        "[(2/7) Slots (5/216) Ques Taken]")


class TheAdvertIsRead(unittest.TestCase):
    def test_omenserve(self):
        advert = irc.parse_channel_advert(OMEN)
        self.assertEqual((advert["slots_free"], advert["slots_total"]), (3, 10))
        self.assertEqual(advert["queued"], 12)
        self.assertEqual(advert["speed"], "45000cps")
        self.assertEqual(advert["mode"], "Servers Only")

    def test_spqr_counts_slots_in_use(self):
        advert = irc.parse_channel_advert(SPQR)
        self.assertEqual((advert["slots_in_use"], advert["slots_total"]), (2, 7))
        self.assertNotIn("slots_free", advert)
        self.assertEqual(advert["queued"], 5)

    def test_what_a_bot_did_not_say_is_absent(self):
        advert = irc.parse_channel_advert("Type: @QuietBot For My List Of: 1,000 Files <> List: Aug 10th")
        for field in ("slots_free", "slots_total", "queued", "speed", "mode"):
            self.assertNotIn(field, advert)


class ShownForOnlineBots(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        entry = {"nick": "PackBot", "last_seen": time.time(), "files": 719041, "list_date": "Aug 10th",
                 "slots_free": 3, "slots_total": 10, "queued": 12, "speed": "45000cps", "mode": "Servers Only"}
        runtime.known_bots["packbot"] = entry
        self.addCleanup(runtime.known_bots.pop, "packbot", None)

    def test_online(self):
        live = webserver._advert_live(runtime.known_bots, "PackBot", {"packbot"})
        self.assertEqual(live, {"slots_free": 3, "slots_total": 10, "queued": 12,
                                "speed": "45000cps", "mode": "Servers Only"})

    def test_not_for_a_bot_that_left(self):
        self.assertEqual(webserver._advert_live(runtime.known_bots, "PackBot", {"someoneelse"}), {})
        self.assertEqual(webserver._advert_live(runtime.known_bots, "PackBot", set()), {})

    def test_a_number_javascript_cannot_carry_is_left_out(self):
        runtime.known_bots["packbot"]["queued"] = 10 ** 30
        self.assertNotIn("queued", webserver._advert_live(runtime.known_bots, "PackBot", {"packbot"}))

    def test_freshness_still_compares_only_files_and_date(self):
        self.assertEqual(list_fetch._advert_snapshot("PackBot"), {"files": 719041, "list_date": "Aug 10th"})

    def test_the_registry_keeps_them_from_a_real_advert(self):
        runtime.known_bots.pop("packbot", None)
        irc._record_bot("packbot", "PackBot", "#chan", irc.parse_channel_advert(OMEN), time.time())
        self.assertEqual(runtime.known_bots["packbot"]["queued"], 12)
        self.assertEqual(runtime.known_bots["packbot"]["mode"], "Servers Only")


class ThePageWritesThemAsText(unittest.TestCase):
    def test_textcontent_not_html(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
            code = handle.read()
        at = code.index('stats.className = "bot-row-live";')
        self.assertIn("stats.textContent = live;", code[at:at + 120])


if __name__ == "__main__":
    unittest.main()
