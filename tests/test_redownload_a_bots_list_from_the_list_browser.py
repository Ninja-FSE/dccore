"""#302: "if auto-fetch does not work I want a button called Re-download list."

AUTO_REFETCH_LISTS re-asks a bot for its list on a timer, and ships off. With
it off, or not working, the only way to fetch a bot's list again was to type
its nick into the fetch box. The List Browser now has a Re-download list
button beside Purge, for whichever bot's list is open.

Read from the page's source, like the rest of the dashboard's tests.
"""

import io
import json
import os
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def read(*parts):
    with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def function_body(source, name, until):
    start = source.index("function " + name + "(")
    return source[start:source.index(until, start)]


class TheButton(unittest.TestCase):

    def test_it_is_in_the_page_and_hidden_until_a_list_is_open(self):
        html = read("web", "index.html")
        self.assertIn('id="filelists-redownload-btn" hidden', html)
        self.assertIn('data-i18n="filelists.redownloadList"', html)

    def test_it_follows_the_purge_buttons_rule(self):
        """Only for a list held from another bot: your own list is the
        library, and a bot only seen advertising has nothing to fetch again
        that the fetch box does not already start."""
        body = function_body(read("web", "app.js"), "renderFilelistsPurge", "function purgeCurrentList")
        self.assertIn("again.hidden = button.hidden", body)
        self.assertIn("isOwnSource(source) || !row || !row.held", body)

    def test_it_asks_for_the_bot_not_the_tab(self):
        """The open tab may be the bot's RAR or VIDEO list; the request is for
        the bot's list archive, like the fetch box's."""
        body = function_body(read("web", "app.js"), "redownloadCurrentList", "function purgeCurrentList")
        self.assertIn('"/api/filelists/fetch"', body)
        self.assertIn("row.nick || row.bot || row.label || source", body)
        self.assertIn("{ bot: bot }", body)

    def test_it_says_what_the_server_said_when_refused(self):
        """409 when a fetch is already running, or the bot is not here - the
        server's own sentence, not a generic failure."""
        body = function_body(read("web", "app.js"), "redownloadCurrentList", "function purgeCurrentList")
        self.assertIn("res.data.error", body)
        self.assertIn("showFilelistsFetchStatus(", body)

    def test_the_click_is_wired(self):
        self.assertIn('el.filelistsRedownloadBtn.addEventListener("click", redownloadCurrentList)',
                      read("web", "app.js"))

    def test_the_button_is_not_left_disabled(self):
        body = function_body(read("web", "app.js"), "redownloadCurrentList", "function purgeCurrentList")
        self.assertIn(".finally(", body)
        self.assertIn("filelistsRedownloadBtn.disabled = false", body)


class TheWords(unittest.TestCase):

    def test_every_language_has_all_three(self):
        for lang in ("en", "fr", "es"):
            with io.open(os.path.join(REPO_ROOT, "web", "lang", lang + ".json"),
                         encoding="utf-8") as handle:
                d = json.load(handle)
            for key in ("filelists.redownloadList", "filelists.redownloadTitle",
                        "filelists.redownloadRequested"):
                with self.subTest(lang=lang, key=key):
                    self.assertTrue(d.get(key, "").strip())
            self.assertIn("{bot}", d["filelists.redownloadRequested"], lang)


if __name__ == "__main__":
    unittest.main()
