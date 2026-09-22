"""#603: the setup page starts with the dashboard box ticked.

defaults.py ships WEBUI_ENABLED = False, and the page read that as-is: on
every first run the "Enable web dashboard" box rendered unticked while the
intro, the folder placeholder and the folder error all told the operator to
choose the music folder "later on the dashboard's Settings page". A novice
who took that advice and did not notice the box ended with a bot that
served nothing and had no Settings page to fix it from.

A fresh page now ticks the box (loopback only, as before - the LAN box is
still separate); a redisplay after an error keeps what the operator
chose; and the folder error names settings.conf when the box is off.
"""

import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_set_it_up_in_the_browser import GOOD, NEEDS_FLASK  # noqa: E402

BOX = re.compile(r'<input type="checkbox" name="WEBUI_ENABLED" value="1"( checked)?>')


def box_is_ticked(html):
    match = BOX.search(html)
    assert match, "the page has no dashboard box"
    return bool(match.group(1))


class TheFreshPage(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        # the shipped default, which is what a first run has
        self.set_config(WEBUI_ENABLED=False)

    def test_the_field_starts_true_although_the_shipped_default_is_false(self):
        by_name = {f["name"]: f for f in webserver.build_setup_fields()}
        self.assertIs(by_name["WEBUI_ENABLED"]["value"], True)
        self.assertIs(config.WEBUI_ENABLED, False, "the shipped default itself is untouched")

    def test_the_rendered_box_is_ticked(self):
        html = webserver.render_setup_page(webserver.build_setup_fields(), "t")
        self.assertTrue(box_is_ticked(html))

    def test_in_every_language(self):
        for lang in webserver.SETUP_LANGS:
            html = webserver.render_setup_page(webserver.build_setup_fields(lang), "t", lang)
            self.assertTrue(box_is_ticked(html), lang)


class TheRedisplay(DCCoreTestCase):
    """After an error the page shows what was chosen, not the fresh default.
    An unticked box is simply absent from a browser's POST, so the typed
    values then carry no WEBUI_ENABLED key at all."""

    def setUp(self):
        super().setUp()
        self.set_config(WEBUI_ENABLED=False)

    def redisplay(self, values):
        return webserver.render_setup_page(webserver.build_setup_fields("en", values),
                                           "t", values=values)

    def test_an_unticked_box_stays_unticked(self):
        typed = {"NICKNAME": "", "SERVER": "irc.example.net", "CHANNEL": "#example"}
        self.assertFalse(box_is_ticked(self.redisplay(typed)))

    def test_a_ticked_box_stays_ticked(self):
        typed = {"NICKNAME": "", "SERVER": "irc.example.net", "WEBUI_ENABLED": "1"}
        self.assertTrue(box_is_ticked(self.redisplay(typed)))


class TheFolderError(DCCoreTestCase):
    def missing_folder(self, **overrides):
        form = dict(GOOD, FILE_DIRECTORY=os.path.join(REPO_ROOT, "no-such-folder-603"))
        form.update(overrides)
        _, _, errors = webserver.validate_setup_form(form)
        return dict(errors)["FILE_DIRECTORY"]

    def test_with_the_dashboard_on_it_points_to_the_settings_page(self):
        message = self.missing_folder()
        self.assertIn("Settings page", message)
        self.assertNotIn("settings.conf", message)

    def test_with_the_dashboard_off_it_points_to_the_file(self):
        message = self.missing_folder(WEBUI_ENABLED="")
        self.assertIn("settings.conf", message)
        self.assertIn("FILE_DIRECTORY", message)
        self.assertIn("tick the dashboard box", message)

    def test_the_dashboard_choice_is_still_written(self):
        for typed, expected in (("1", True), ("", False)):
            changes, _, errors = webserver.validate_setup_form(dict(GOOD, WEBUI_ENABLED=typed))
            self.assertEqual(errors, [])
            self.assertIs(changes["WEBUI_ENABLED"], expected)


@unittest.skipUnless(webserver.HAVE_FLASK, NEEDS_FLASK)
class ThroughTheApp(DCCoreTestCase):
    def setUp(self):
        super().setUp()
        self.set_config(WEBUI_ENABLED=False)
        self.client = webserver.create_setup_app("the-token", lambda _: None, port=8420).test_client()

    def get(self, path):
        return self.client.get(path, headers={"Host": "127.0.0.1:8420"}).get_data(as_text=True)

    def post(self, data):
        return self.client.post("/setup", data=data, headers={"Host": "127.0.0.1:8420"}).get_data(as_text=True)

    def test_the_first_get_is_ticked(self):
        self.assertTrue(box_is_ticked(self.get("/setup?token=the-token")))

    def test_a_bad_post_with_the_box_unticked_comes_back_unticked(self):
        data = {k: v for k, v in GOOD.items() if k != "WEBUI_ENABLED"}
        html = self.post(dict(data, token="the-token", NICKNAME=""))
        self.assertFalse(box_is_ticked(html))
        self.assertIn('class="error"', html)


if __name__ == "__main__":
    unittest.main()
