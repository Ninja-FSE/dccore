"""The Settings page groups settings the way an operator looks for them.

From the operator, during the RC1 beta:

    "Settings pages at webpage are a mess. Need better grouping, hiding some
    that are never used like folder locations under advanced settings etc.
    Also why is packet size on different page than buffer size."

All three, and a fourth they did not name. The old grouping is how any
grouping ends up - by accretion. Each feature put its settings wherever there
was room, so of 94 settings:

    Slots & queue      25      DCC slots, queue limits, message delays, DCC
                               ports, fetch slots, fetch history, fetch sizes,
                               the send buffer, the rehash wait, auto-refetch,
                               and four fetch timeouts
    Paths & storage    31      list generation, .rar packing, the PACKET SIZE,
                               and seventeen file paths

Two thirds of the page in two categories, and DCC_BLOCK_SIZE and
DCC_SEND_BUFFER - the pair anyone tuning a transfer reads together, as the
3 MB/s investigation needed - a category apart from each other.

The fourth: the colour theme sat under "Advertising & search", because
announce.py is what draws it. That is a fact about the code, not about what
an operator came looking for.

NOTHING MOVES ON DISK. settings.conf is flat; a category is a grouping for
this page and nothing else. Every value an operator has saved is untouched,
and so is every name - only what they read changed.

These tests assert the RULES rather than the arrangement, so a later regroup
that keeps them passes and one that undoes them does not.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import webserver  # noqa: E402


class SettingsPage(unittest.TestCase):

    def setUp(self):
        self.payload = webserver.build_settings_payload()
        self.categories = self.payload["categories"]

    def by_setting(self):
        return {field["name"]: category["label"]
                for category in self.categories
                for field in category["fields"]}

    def category_of(self, name):
        placed = self.by_setting()
        self.assertIn(name, placed, f"{name} is not on the page at all")
        return placed[name]


class RelatedSettingsAreTogether(SettingsPage):

    def test_the_packet_size_and_the_send_buffer_share_a_category(self):
        """The complaint, verbatim: "why is packet size on different page than
        buffer size". They are the transfer-tuning pair - the investigation
        that found a 3 MB/s ceiling needed both - and were a category apart."""
        self.assertEqual(self.category_of("DCC_BLOCK_SIZE"),
                         self.category_of("DCC_SEND_BUFFER"))

    def test_a_timeout_sits_with_the_one_it_qualifies(self):
        """Reading one without the other tells half a story."""
        self.assertEqual(
            self.category_of("FETCH_FOLDER_OFFER_TIMEOUT"),
            self.category_of("FETCH_FOLDER_OFFER_TIMEOUT_UNADVERTISED"))

    def test_every_fetch_timeout_is_in_one_place(self):
        placed = {name: self.category_of(name) for name in (
            "FETCH_OFFER_TIMEOUT", "FETCH_TRANSFER_TIMEOUT",
            "FETCH_FOLDER_OFFER_TIMEOUT", "FETCH_FOLDER_TRANSFER_TIMEOUT")}

        self.assertEqual(len(set(placed.values())), 1, placed)

    def test_the_rar_settings_are_with_the_list_they_shape(self):
        """RAR_ENABLED decides whether a .rar list is built at all."""
        for name in ("RAR_ENABLED", "RAR_EXTENSIONS", "MAX_RAR_FOLDER_SIZE"):
            self.assertEqual(self.category_of(name),
                             self.category_of("LIST_FORMAT"), name)

    def test_the_theme_is_not_filed_under_advertising(self):
        """It lived there because announce.py draws it - a fact about the
        code rather than about what the operator came looking for."""
        theme = self.category_of("THEME")

        self.assertNotIn("Advertising", theme)
        self.assertEqual(theme, self.category_of("CUSTOM_THEME_ACCENT"))


class TheThingsNobodyChangesAreOutOfTheWay(SettingsPage):

    def advanced(self):
        found = [c for c in self.categories if c["label"].startswith("Advanced")]
        self.assertEqual(len(found), 1, "expected exactly one Advanced section")
        return found[0]

    def test_there_is_an_advanced_section(self):
        self.assertTrue(self.advanced()["fields"])

    def test_it_is_last(self):
        """So nobody opens it by accident on the way to something else."""
        self.assertEqual(self.categories[-1]["label"],
                         self.advanced()["label"])

    def test_the_file_locations_are_in_it(self):
        """Set once at install. A wrong value here loses a queue or a
        statistics file rather than mis-tuning something."""
        for name in ("BANS_FILE", "STATS_FILE", "LIST_INDEX_FILE",
                     "TMP_ZIP_DIR", "LOCAL_LIST_DIR", "FETCHED_FILES_DIR"):
            self.assertEqual(self.category_of(name),
                             self.advanced()["label"], name)

    def test_nothing_changed_weekly_is_in_it(self):
        """The other half of the rule: hiding something an operator DOES
        reach for would be worse than the mess it replaced."""
        for name in ("MAX_DCC_SLOTS", "CHANNEL", "THEME", "RAR_ENABLED",
                     "DCC_SEND_BUFFER", "WEBUI_ENABLED"):
            self.assertNotEqual(self.category_of(name),
                                self.advanced()["label"], name)


class NoCategoryIsADumpingGround(SettingsPage):

    def test_no_ordinary_category_is_enormous(self):
        """"Slots & queue" reached 25 and "Paths & storage" 31 - two thirds of
        the page in two categories, which is how a grouping stops being one.

        The Advanced section is exempt: it exists precisely to hold the long
        tail, and its size is the point rather than a problem."""
        for category in self.categories:
            if category["label"].startswith("Advanced"):
                continue
            with self.subTest(category=category["label"]):
                self.assertLessEqual(
                    len(category["fields"]), 16,
                    "this category is becoming the next dumping ground")

    def test_every_category_has_something_in_it(self):
        """An empty heading is a category that lost its contents to a rename,
        and the completeness guard would not notice - it checks that every
        SETTING is placed, not that every category is used."""
        for category in self.categories:
            with self.subTest(category=category["label"]):
                self.assertTrue(category["fields"])

    def test_no_setting_is_in_two_places(self):
        """A duplicate renders twice and saves twice, and the two copies
        disagree the moment one is edited."""
        seen = []
        for category in self.categories:
            seen.extend(field["name"] for field in category["fields"])

        self.assertEqual(sorted(seen), sorted(set(seen)))


class TheGroupingIsOnlyAGrouping(SettingsPage):
    """settings.conf is flat. Moving a setting between categories must change
    what an operator READS and nothing else."""

    def test_every_setting_still_carries_its_own_name(self):
        """The name is what settings.conf stores and what a hand-edited file
        holds. A category cannot rename one."""
        import settings_file
        import defaults as config

        declared = set(settings_file.declared_types(vars(config)))
        placed = set(self.by_setting())

        self.assertTrue(placed <= declared,
                        f"the page offers settings config.py does not "
                        f"declare: {sorted(placed - declared)}")

    def test_the_page_still_offers_effectively_all_of_them(self):
        """Guard on the guard: every assertion above is satisfied by a page
        showing three settings in tidy categories."""
        self.assertGreater(len(self.by_setting()), 80)


if __name__ == "__main__":
    unittest.main()
