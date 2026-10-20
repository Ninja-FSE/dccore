"""#1309 (was #875): the project page in site/, published from gh-pages.

site/ is the source of the project page. It never reaches the public tree
(export-ignore): the page is copied by hand to the orphan gh-pages branch of
the public repository, which GitHub Pages serves, so a clone of that
repository's main holds no website (docs/PUBLIC-REPO-WORKFLOW.md, step 5).

What the pages must hold to, checked on the files themselves:

  * every link resolves - to another page of the site, or to a file that is
    in the PUBLIC tree, where docs/UPDATES-PUBLIC.md has become
    docs/UPDATES.md and the export-ignored files are gone;
  * they make no third-party requests: no scripts, fonts, images or frames,
    and no stylesheet but the site's own, so opening a page tells nobody
    anything;
  * every page carries the same menu and footer, the menu naming every page
    and marking the one it is on;
  * site/ stays out of `git archive`, and no workflow publishes it.

The identifier sweep (test_no_personal_identifiers_ship.py) already scans
.html and .css, so a picture with a real channel or nick in it fails there.
"""

import io
import os
import re
import subprocess
import sys
import tarfile
import unittest
from html.parser import HTMLParser

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from exported_tree import internal_file_or_skip  # noqa: E402

PUBLIC_REPO = "https://github.com/Ninja-FSE/dccore"

# The menu's order is the site's order. Adding a page is adding it here.
PAGES = ("index.html", "commands.html", "dashboard.html", "mirc.html", "start.html")

# What extraction renames: the public changelog ships under the internal one's
# name (docs/PUBLIC-REPO-WORKFLOW.md, step 3).
RENAMED_AT_EXTRACTION = {"docs/UPDATES.md": "docs/UPDATES-PUBLIC.md"}


def read(path):
    with io.open(path, encoding="utf-8") as handle:
        return handle.read()


class _Collect(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def parsed(text):
    collector = _Collect()
    collector.feed(text)
    return collector.tags


def export_ignored():
    ignored = set()
    for line in read(os.path.join(REPO_ROOT, ".gitattributes")).splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.lstrip().startswith("#") and "export-ignore" in parts[1:]:
            ignored.add(parts[0])
    return ignored


def ignores(rule, path):
    """Whether an export-ignore pattern covers `path`: the path itself, a
    directory above it, or a `dir/**` pattern."""
    rule = rule.lstrip("/")
    if rule.endswith("/**"):
        rule = rule[:-3]
    return path == rule or path.startswith(rule + "/")


def between(text, start, end):
    """The text from the first `start` to the first `end` after it, both kept."""
    begin = text.index(start)
    return text[begin:text.index(end, begin) + len(end)]


class SiteTestCase(unittest.TestCase):

    def setUp(self):
        self.site = os.path.dirname(internal_file_or_skip(self, os.path.join("site", "index.html")))

    def page(self, name):
        return read(os.path.join(self.site, name))

    def links(self, name):
        return [attrs["href"] for tag, attrs in parsed(self.page(name)) if tag == "a" and "href" in attrs]

    def all_links(self):
        return [(name, href) for name in PAGES for href in self.links(name)]


class EveryPageIsThere(SiteTestCase):

    def test_the_site_is_exactly_its_pages_and_stylesheet(self):
        present = set(os.listdir(self.site))
        self.assertEqual(present, set(PAGES) | {"style.css", ".nojekyll"})

    def test_each_is_one_complete_page(self):
        for name in PAGES:
            page = self.page(name)
            with self.subTest(page=name):
                self.assertTrue(page.startswith("<!doctype html>"))
                self.assertIn('<html lang="en">', page)
                self.assertRegex(page, r"<title>[^<]+</title>")
                self.assertRegex(page, r'<meta name="description" content="[^"]+">')
                self.assertIn('name="viewport"', page)


class EveryLinkResolves(SiteTestCase):

    def test_there_are_links_to_check(self):
        self.assertGreaterEqual(len(self.all_links()), 60)

    def test_each_goes_to_a_page_of_the_site_or_to_the_public_repository(self):
        for name, href in self.all_links():
            with self.subTest(page=name, href=href):
                self.assertTrue(href in PAGES or href == PUBLIC_REPO or href.startswith(PUBLIC_REPO + "/"),
                                "a link out of the project, or to a page the site does not have")

    def test_a_file_link_names_a_file_the_public_tree_has(self):
        ignored = export_ignored()
        checked = set()
        for name, href in self.all_links():
            found = re.match(re.escape(PUBLIC_REPO) + r"/blob/main/([^#]+)(#[a-z0-9-]+)?$", href)
            if not found:
                continue
            path = found.group(1)
            source = RENAMED_AT_EXTRACTION.get(path, path)
            with self.subTest(page=name, path=path):
                self.assertTrue(os.path.isfile(os.path.join(REPO_ROOT, source)), f"{source} is not in the tree")
                if path not in RENAMED_AT_EXTRACTION:
                    self.assertFalse(any(ignores(rule, path) for rule in ignored),
                                     "export-ignored: the public tree does not have it")
            checked.add(path)
        self.assertGreaterEqual(len(checked), 6)

    def test_any_other_repository_link_is_a_page_github_has(self):
        allowed = {PUBLIC_REPO, PUBLIC_REPO + "/releases", PUBLIC_REPO + "/releases/latest",
                   # the mIRC script's zip, a release asset with a fixed name (#1412)
                   PUBLIC_REPO + "/releases/latest/download/dccore-mirc.zip"}
        for name, href in self.all_links():
            if href.startswith(PUBLIC_REPO) and "/blob/main/" not in href:
                with self.subTest(page=name, href=href):
                    self.assertIn(href, allowed)


class NothingIsLoadedFromAnywhere(SiteTestCase):

    def test_no_tag_loads_anything_but_the_sites_own_stylesheet(self):
        for name in PAGES:
            for tag, attrs in parsed(self.page(name)):
                with self.subTest(page=name, tag=tag):
                    self.assertNotIn(tag, ("script", "iframe", "object", "embed", "img", "source", "video",
                                           "audio", "style"), f"<{tag}> on {name}: {attrs}")
                    if tag == "link":
                        self.assertEqual(attrs, {"rel": "stylesheet", "href": "style.css"})

    def test_no_attribute_fetches_anything(self):
        for name in PAGES:
            for tag, attrs in parsed(self.page(name)):
                for attribute, value in attrs.items():
                    with self.subTest(page=name, tag=tag, attribute=attribute):
                        self.assertNotIn(attribute, ("src", "srcset", "poster", "data", "background"))
                        if attribute == "style":
                            self.assertNotIn("url(", value)

    def test_the_stylesheet_fetches_nothing(self):
        style = self.page("style.css")
        self.assertGreater(len(style), 1000)
        self.assertNotIn("@import", style)
        self.assertNotIn("url(", style)
        self.assertNotIn("@font-face", style)


class EveryPageHasTheSameMenuAndFooter(SiteTestCase):

    def menu(self, name):
        return between(self.page(name), '<header class="top', "</header>")

    def footer(self, name):
        return between(self.page(name), "<footer", "</footer>")

    def test_the_menu_names_every_page_in_order(self):
        for name in PAGES:
            nav = between(self.menu(name), "<nav", "</nav>")
            with self.subTest(page=name):
                self.assertEqual(tuple(re.findall(r'href="([^"]+)"', nav)), PAGES)

    def test_the_menu_marks_the_page_it_is_on_and_no_other(self):
        for name in PAGES:
            with self.subTest(page=name):
                self.assertEqual(re.findall(r'<a href="([^"]+)" aria-current="page">', self.menu(name)), [name])
                self.assertEqual(self.menu(name).count("aria-current"), 1)

    def test_the_menu_is_otherwise_the_same_everywhere(self):
        first = self.menu(PAGES[0]).replace(' aria-current="page"', "")
        for name in PAGES[1:]:
            with self.subTest(page=name):
                self.assertEqual(self.menu(name).replace(' aria-current="page"', ""), first)

    def test_the_footer_is_the_same_everywhere(self):
        first = self.footer(PAGES[0])
        for name in PAGES[1:]:
            with self.subTest(page=name):
                self.assertEqual(self.footer(name), first)


class TheVersionsOnThePageAreTheReleases(SiteTestCase):
    """#1430: the overview says which version "Download the latest release"
    is, and which script the mIRC download holds. Written into the page (it
    loads nothing), so these hold it to the one place each fact lives: the
    bot's SCRIPT_VERSION and the script's dccore.ver. A release roll that
    bumps either fails here until the page follows."""

    def version(self, which):
        found = re.findall(r'data-version="%s">([^<]+)<' % which, self.page("index.html"))
        self.assertEqual(len(found), 1, f"one {which} version on the page")
        return found[0]

    def test_the_bots_version_is_SCRIPT_VERSION(self):
        import defaults
        shipped = re.search(r"v\d+\.\d+\.\d+", defaults.SCRIPT_VERSION).group(0)
        self.assertEqual(self.version("bot"), shipped)

    def test_the_scripts_version_is_dccore_ver(self):
        sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
        try:
            import build_mirc_zip
        finally:
            sys.path.remove(os.path.join(REPO_ROOT, "scripts"))
        self.assertEqual(self.version("mirc"), "dccore.mrc " + build_mirc_zip.script_version())

    def test_the_script_link_is_the_release_asset(self):
        self.assertIn(PUBLIC_REPO + "/releases/latest/download/dccore-mirc.zip", self.links("index.html"))


class TheSiteStaysOutOfThePublicTree(SiteTestCase):

    def test_site_has_an_export_ignore_rule(self):
        self.assertTrue(any(ignores(rule, "site/index.html") for rule in export_ignored()))

    def test_git_archive_leaves_site_behind(self):
        try:
            result = subprocess.run(["git", "archive", "--format=tar", "HEAD"],
                                    cwd=REPO_ROOT, capture_output=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.skipTest(f"git archive could not run here: {exc}")
        if result.returncode != 0 or not result.stdout:
            self.skipTest("git archive produced nothing here (no git, or no commit)")
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            names = [n.replace("\\", "/") for n in archive.getnames()]
        self.assertIn("oserve.py", names)
        self.assertEqual([n for n in names if n == "site" or n.startswith("site/")], [])

    def test_no_workflow_publishes_it(self):
        """gh-pages is published by hand; a Pages workflow would put the site
        back on the public main's path, or fail there for want of site/."""
        workflows = os.path.join(REPO_ROOT, ".github", "workflows")
        for name in sorted(os.listdir(workflows)):
            with self.subTest(workflow=name):
                text = read(os.path.join(workflows, name))
                self.assertNotIn("deploy-pages", text)
                self.assertNotIn("upload-pages-artifact", text)


if __name__ == "__main__":
    unittest.main()
