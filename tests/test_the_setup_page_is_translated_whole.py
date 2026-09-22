"""#621: the setup page is translated whole, not only its field labels.

The FR/ES switch used to translate the six field labels and their "?" help
(the settings.field.* keys the Settings page already had) and nothing else:
the title, the intro, both password labels, the LAN box, the placeholders,
the submit button, the note, every validation error and the Saved page were
looked up under setup.* keys that no lang file defined, and the errors were
English literals with no language at all. A French operator got a
mixed-language form, and an English error under a French label.

Now every setup.* key webserver.py looks up exists in en, fr and es (the
translations test enforces the parity), validate_setup_form() takes the
language, and the route passes it on. The nickname problem itself (what
settings_file.nick_problem() says is wrong with "Music Bot") stays English:
it is the terminal's text too, and translating it is a different piece of
work.
"""

import io
import json
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_set_it_up_in_the_browser import GOOD, NEEDS_FLASK  # noqa: E402

TRANSLATED = ("fr", "es")

# The English fallbacks that used to show through in every language.
ENGLISH_PAGE = ("Set up DCCore", "Admin password", "The same password again",
                "Reachable from other devices", "Save and start the bot",
                "A few questions and the bot can start", "Only this machine can reach this page",
                "#mychannel, #another", "can be chosen later on the Settings page",
                "Kept as a hash, never in clear")

REFUSALS = {
    "blank nick": dict(NICKNAME=""), "nick with a space": dict(NICKNAME="Music Bot"),
    "blank server": dict(SERVER=""), "server with a space": dict(SERVER="irc example"),
    "no channel": dict(CHANNEL=""), "channel without #": dict(CHANNEL="example"),
    "blank admin": dict(ADMIN_NICK=""), "admin with a space": dict(ADMIN_NICK="Sys Op"),
    "admin host with a wildcard": dict(ADMIN_HOST="*.users.undernet.org"),
    "blank password": dict(password="", password_confirm=""),
    "mismatch": dict(password_confirm="something else"),
    "missing folder": dict(FILE_DIRECTORY=os.path.join(tempfile.gettempdir(), "no-such-dccore-folder-621")),
    "missing folder, dashboard off": dict(FILE_DIRECTORY=os.path.join(tempfile.gettempdir(), "no-such-dccore-folder-621"),
                                          WEBUI_ENABLED=""),
}


def strings(lang):
    with io.open(os.path.join(REPO_ROOT, "web", "lang", f"{lang}.json"), encoding="utf-8") as handle:
        return json.load(handle)


def english_errors():
    return {message for overrides in REFUSALS.values()
            for _, message in webserver.validate_setup_form(dict(GOOD, **overrides))[2]}


# Refusals whose message carries an English explanation (from
# settings_file.nick_problem()/admin_host_problem()) after the translated
# prefix - a different piece of work than translating the setup page itself,
# same as the nickname problem the module docstring already excepts.
PREFIX_PROBLEMS = {
    "nick with a space": "setup.error.nickname_invalid",
    "admin with a space": "setup.error.nickname_invalid",
    "admin host with a wildcard": "setup.error.admin_host_shape",
}


class TheForm(DCCoreTestCase):
    def test_the_whole_page_is_in_the_chosen_language(self):
        for lang in TRANSLATED:
            words = strings(lang)
            html = webserver.render_setup_page(webserver.build_setup_fields(lang), "t", lang)
            with self.subTest(lang):
                for english in ENGLISH_PAGE:
                    self.assertNotIn(english, html, english)
                for key in ("setup.title", "setup.intro", "setup.password", "setup.password_help",
                            "setup.password_again", "setup.lan", "setup.submit", "setup.note",
                            "setup.channel_placeholder", "setup.folder_placeholder"):
                    self.assertIn(webserver._html(words[key]), html, key)

    def test_english_is_still_english(self):
        html = webserver.render_setup_page(webserver.build_setup_fields("en"), "t", "en")
        for english in ENGLISH_PAGE:
            self.assertIn(english, html, english)


class TheErrors(DCCoreTestCase):
    def test_each_refusal_is_in_the_chosen_language(self):
        """Every message validate_setup_form() can produce for fr/es is a
        setup.error.* value from that lang file, and none is the English
        one; the nickname problem inside the message is the one exception."""
        english = english_errors()
        for lang in TRANSLATED:
            words = strings(lang)
            translated = {v for k, v in words.items() if k.startswith("setup.error.")}
            for label, overrides in REFUSALS.items():
                with self.subTest(lang=lang, refusal=label):
                    _, _, errors = webserver.validate_setup_form(dict(GOOD, **overrides), lang)
                    self.assertEqual(len(errors), 1, errors)
                    message = errors[0][1]
                    self.assertNotIn(message, english, message)
                    if label in PREFIX_PROBLEMS:
                        prefix = words[PREFIX_PROBLEMS[label]].split("{problem}")[0]
                        self.assertTrue(message.startswith(prefix), message)
                    else:
                        self.assertIn(message, translated, message)

    def test_the_default_language_is_english(self):
        """Callers that pass no language - the existing tests, and the
        terminal path this function does not serve - see the same messages
        as before."""
        _, _, errors = webserver.validate_setup_form(dict(GOOD, NICKNAME=""))
        self.assertEqual(errors, [("NICKNAME", "A nickname is needed.")])
        _, _, errors = webserver.validate_setup_form(dict(GOOD, NICKNAME="Music Bot"), "en")
        self.assertTrue(errors[0][1].startswith("That is not an IRC nickname: "), errors)

    def test_the_fixture_covers_every_error_key(self):
        """Fixture invariant: REFUSALS drives every setup.error.* key but
        the write failure (which needs a broken disk; ThroughTheApp below
        fakes it), so a new key with no refusal here cannot pass unseen."""
        driven = [message for overrides in REFUSALS.values()
                  for _, message in webserver.validate_setup_form(dict(GOOD, **overrides), "fr")[2]]
        words = strings("fr")
        unseen = sorted(key for key, value in words.items()
                        if key.startswith("setup.error.") and key != "setup.error.write"
                        and not any(message.startswith(value.split("{problem}")[0]) for message in driven))
        self.assertEqual(unseen, [])


class TheSavedPage(DCCoreTestCase):
    def test_both_versions_are_in_the_chosen_language(self):
        for lang in TRANSLATED:
            words = strings(lang)
            with self.subTest(lang):
                html = webserver.render_setup_saved_page({"WEBUI_ENABLED": True}, lang)
                self.assertIn(webserver._html(words["setup.saved.title"]), html)
                self.assertIn(webserver._html(words["setup.saved.dashboard"]), html)
                self.assertNotIn("Saved - starting the bot", html)
                html = webserver.render_setup_saved_page({"WEBUI_ENABLED": False}, lang)
                self.assertIn(webserver._html(words["setup.saved.no_dashboard"]), html)
                self.assertNotIn("Close this tab", html)


@unittest.skipUnless(webserver.HAVE_FLASK, NEEDS_FLASK)
class ThroughTheApp(DCCoreTestCase):
    """The route has to pass the language to the validator too - the
    functions above being right is not enough."""

    def setUp(self):
        super().setUp()
        self.client = webserver.create_setup_app("the-token", lambda _: None, port=8420).test_client()

    def post(self, data):
        response = self.client.post("/setup", data=data, headers={"Host": "127.0.0.1:8420"})
        return response.status_code, response.get_data(as_text=True)

    def test_a_bad_french_post_comes_back_with_french_errors(self):
        status, html = self.post(dict(GOOD, token="the-token", lang="fr", NICKNAME="", password_confirm="x"))
        self.assertEqual(status, 400)
        words = strings("fr")
        self.assertIn(webserver._html(words["setup.error.nickname_needed"]), html)
        self.assertIn(webserver._html(words["setup.error.password_mismatch"]), html)
        self.assertNotIn("A nickname is needed.", html)
        self.assertNotIn("The two passwords do not match.", html)

    def test_a_write_failure_is_reported_in_the_chosen_language(self):
        def broken(changes, password_hash, log=print):
            raise OSError("disk full")
        real = webserver.apply_setup
        webserver.apply_setup = broken
        self.addCleanup(setattr, webserver, "apply_setup", real)
        status, html = self.post(dict(GOOD, token="the-token", lang="es"))
        self.assertEqual(status, 500)
        expected = strings("es")["setup.error.write"].replace("{error}", "disk full")
        self.assertIn(webserver._html(expected), html)
        self.assertNotIn("Could not write the settings", html)


if __name__ == "__main__":
    unittest.main()
