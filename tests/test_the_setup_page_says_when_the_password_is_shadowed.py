"""The setup page never said that a hash already in settings.conf would win
over the one it had just written (audit L12, #676).

The first half of the finding - settings.conf written before admin_config.py,
so a failed second write left a configured bot with no password and no way
back to the page - was closed by #624: admin_config.py is written first.
This is the other half. defaults.py applies admin_config.py first and
settings.conf second, so a pre-existing ADMIN_PASSWORD_HASH in settings.conf
(the dashboard's own change-password control writes there) overrides the
hash the setup form writes - after the next restart, the password the
operator just chose stops working. The shared writer prints the warning to
the daemon's window, where configure.py's operator is; the person at the
form is in a browser and never saw it. apply_setup() returns what the
writer found and the saved page says it, in every language it has.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import settings_file  # noqa: E402
import webserver  # noqa: E402

from tests import test_set_it_up_in_the_browser as setup  # noqa: E402

GOOD = setup.GOOD
SHADOW_LINE = "ADMIN_PASSWORD_HASH = pbkdf2_sha256$1000$aaaa$bbbb\n"


def with_a_hash_in_settings_conf(case):
    """settings.conf, where the daemon will read it, already carrying a hash.
    The harness points DCCORE_SETTINGS_FILE at a per-test path."""
    path = settings_file.settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write("NICKNAME = OldBot\n" + SHADOW_LINE)
    case.addCleanup(lambda: os.path.exists(path) and os.remove(path))
    return path


class ApplyingItReportsTheShadow(setup.ApplyingIt):

    def apply(self):
        changes, password_hash, _ = webserver.validate_setup_form(GOOD)
        return webserver.apply_setup(changes, password_hash, log=lambda *_: None,
                                     settings_path=self.settings, admin_path=self.admin)

    def test_a_hash_already_in_settings_conf_is_named(self):
        with_a_hash_in_settings_conf(self)

        result = self.apply()

        self.assertEqual(result["shadowed_by"], "settings.conf")

    def test_and_none_when_there_is_none(self):
        result = self.apply()

        self.assertIsNone(result["shadowed_by"])
        self.assertEqual(result["written"], sorted(webserver.validate_setup_form(GOOD)[0]))


for _name in [n for n in dir(setup.ApplyingIt) if n.startswith("test")]:
    setattr(ApplyingItReportsTheShadow, _name, None)


@unittest.skipUnless(webserver.HAVE_FLASK, setup.NEEDS_FLASK)
class TheSavedPageSaysSo(setup.TheApp):

    def save(self, lang="en"):
        self.get("/setup?token=the-token")
        return self.post(dict(GOOD, token="the-token", lang=lang))

    def test_the_saved_page_warns_when_settings_conf_holds_a_hash(self):
        with_a_hash_in_settings_conf(self)

        resp = self.save()

        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn('<p class="warn">', page)
        self.assertIn("settings.conf also sets ADMIN_PASSWORD_HASH", page)
        self.assertIn("password you just chose will stop working", page)
        self.assertIn("Remove the ADMIN_PASSWORD_HASH line from settings.conf", page)

    def test_the_saved_page_reloaded_still_says_it(self):
        with_a_hash_in_settings_conf(self)
        self.save()

        page = self.get("/setup").data.decode("utf-8")

        self.assertIn("settings.conf also sets ADMIN_PASSWORD_HASH", page)

    def test_and_says_nothing_when_there_is_nothing_to_say(self):
        page = self.save().data.decode("utf-8")

        self.assertNotIn('class="warn"', page)
        self.assertNotIn("ADMIN_PASSWORD_HASH", page)

    def test_in_spanish_and_french_too(self):
        with_a_hash_in_settings_conf(self)
        self.get("/setup?token=the-token")

        for lang, must in (("es", "también define ADMIN_PASSWORD_HASH"),
                           ("fr", "définit aussi ADMIN_PASSWORD_HASH")):
            page = self.post(dict(GOOD, token="the-token", lang=lang)).data.decode("utf-8") \
                if lang == "es" else self.get("/setup?lang=fr").data.decode("utf-8")

            self.assertIn(must, page, lang)
            self.assertIn("settings.conf", page, lang)
            self.assertNotIn("{file}", page, lang)


class TheStringsAreThere(unittest.TestCase):

    def test_every_language_file_has_the_key_with_the_placeholder(self):
        import json
        for lang in ("en", "es", "fr"):
            with io.open(os.path.join(REPO_ROOT, "web", "lang", lang + ".json"), encoding="utf-8") as handle:
                strings = json.load(handle)

            self.assertIn("{file}", strings["setup.saved.shadowed"], lang)
            self.assertIn("ADMIN_PASSWORD_HASH", strings["setup.saved.shadowed"], lang)


for _name in [n for n in dir(setup.TheApp) if n.startswith("test")]:
    setattr(TheSavedPageSaysSo, _name, None)


if __name__ == "__main__":
    unittest.main()
