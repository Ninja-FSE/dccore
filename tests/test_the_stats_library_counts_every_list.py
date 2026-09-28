"""The Stats page's Library block counts every served list, not the primary alone (#952).

`build_stats_payload()` read the library without saying which list: no name to
`get_file_count_date_size_and_raw_bytes()`, so the primary's files and side
files, and `count_rar_album_folders()` looked only in LOCAL_LIST_DIR. On a bot
serving a music list and a film list the page showed the music list's files,
size and album folders as if they were the library - and the channel adverts,
which do ask per list, disagreed with it.

Now the four Library figures are totals across every list, `library.lists`
holds one row per list, and a single-list install reads exactly as before.

Two halves. The payload tests build a real two-list install on disk. The page
tests read app.js, and run the real `renderLibraryLists()` under node against a
stub DOM - a guard that reads text cannot tell that the table is hidden for one
list and shown for two - skipped where node is not installed.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

GIB = 1024 ** 3


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class TwoLists(DCCoreTestCase):
    """A primary list of 3 files and a second one of 2, each with its own side
    files - the layout update_list.py writes: the primary in LOCAL_LIST_DIR
    itself, every other list in a directory of its own."""

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-statslib-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lists_dir = os.path.join(self.tmp, "lists")
        os.makedirs(self.lists_dir)
        self.set_config(LOCAL_LIST_DIR=self.lists_dir, NICKNAME="SomeBot",
                        LIST_BASE_NAME="SomeBot")
        self.write_lists_file([
            {"name": "music", "primary": True, "channels": [], "folders": []},
            {"name": "video", "primary": False, "channels": ["#somechannel"], "folders": []},
        ])
        self.video_dir = list_mod.list_dir("video")
        self.assertNotEqual(self.video_dir, self.lists_dir, "the second list has its own directory")

        self.build("music", self.lists_dir, files=3, raw=GIB + GIB // 2, size="1.50GB")
        self.build("video", self.video_dir, files=2, raw=GIB // 2, size="512.00MB")

    def write_lists_file(self, entries):
        path = os.path.join(self.tmp, "lists.json")
        write(path, json.dumps(entries))
        self.set_config(LISTS_FILE=path)

    def build(self, name, directory, files, raw, size, rar=None, when=None):
        """What one rebuild leaves in a list's directory."""
        master = os.path.join(directory, "SomeBot-2026-09-07.txt")
        write(master, "List of %d Files\n" % files
              + "".join("!SomeBot Track %s %d.mp3  ::INFO:: 1.0MB\n" % (name, n) for n in range(files)))
        write(os.path.join(directory, config.LIST_SIZE_FILE), size)
        write(os.path.join(directory, config.LIST_RAWBYTES_FILE), str(raw))
        if rar is not None:
            write(os.path.join(directory, "SomeBot-RAR-2026-09-07.txt"),
                  "List of Entire Album Folders\nx\n" + "=" * 20 + "\n\n"
                  + "".join("!SomeBot !rar D:\\%s\\Album %d\\\n" % (name, n) for n in range(rar)))
        if when is not None:
            os.utime(master, (when, when))
        return master


class TheTotalsCoverEveryList(TwoLists):

    def test_the_files_are_summed(self):
        self.assertEqual(webserver.build_library_payload()["files"], 5)

    def test_the_bytes_are_summed_and_the_size_is_written_from_the_sum(self):
        library = webserver.build_library_payload()

        self.assertEqual(library["raw_bytes"], 2 * GIB)
        self.assertEqual(library["size"], "2.00GB",
                         "the total, in the two-decimal style the lists' own size files use")

    def test_the_album_folders_are_summed_over_the_lists_that_have_a_rar_list(self):
        self.build("music", self.lists_dir, files=3, raw=GIB, size="1.00GB", rar=7)
        self.build("video", self.video_dir, files=2, raw=GIB, size="1.00GB", rar=4)

        self.assertEqual(webserver.build_library_payload()["rar_folders"], 11)

    def test_a_list_with_no_rar_list_adds_nothing_but_does_not_make_the_total_unknown(self):
        self.build("music", self.lists_dir, files=3, raw=GIB, size="1.00GB", rar=7)

        library = webserver.build_library_payload()

        self.assertEqual(library["rar_folders"], 7)
        video = [row for row in library["lists"] if row["name"] == "video"][0]
        self.assertIsNone(video["rar_folders"], "unknown for that list, not zero")

    def test_no_rar_list_anywhere_is_unknown_not_zero(self):
        self.assertIsNone(webserver.build_library_payload()["rar_folders"])

    def test_the_list_built_date_is_the_newest_build(self):
        older, newer = 1_600_000_000, 1_800_000_000
        self.build("music", self.lists_dir, files=3, raw=GIB, size="1.00GB", when=older)
        self.build("video", self.video_dir, files=2, raw=GIB, size="1.00GB", when=newer)

        library = webserver.build_library_payload()

        by_name = {row["name"]: row for row in library["lists"]}
        self.assertNotEqual(by_name["music"]["list_date"], by_name["video"]["list_date"])
        self.assertEqual(library["list_date"], by_name["video"]["list_date"])


class EachListHasItsOwnRow(TwoLists):

    def test_one_row_per_list_in_the_operators_order_with_its_own_figures(self):
        rows = webserver.build_library_payload()["lists"]

        self.assertEqual([row["name"] for row in rows], ["music", "video"])
        self.assertEqual([row["primary"] for row in rows], [True, False])
        self.assertEqual([row["files"] for row in rows], [3, 2])
        self.assertEqual([row["size"] for row in rows], ["1.50GB", "512.00MB"])
        self.assertEqual([row["raw_bytes"] for row in rows], [GIB + GIB // 2, GIB // 2])

    def test_the_primary_is_asked_for_with_no_name_and_the_other_by_name(self):
        """The primary keeps the path the page has always taken; only a list
        that is not the primary is named."""
        asked = []
        real = list_mod.get_file_count_date_size_and_raw_bytes

        def spy(name=None):
            asked.append(name)
            return real(name)

        list_mod.get_file_count_date_size_and_raw_bytes = spy
        self.addCleanup(setattr, list_mod, "get_file_count_date_size_and_raw_bytes", real)

        webserver.build_library_payload()

        self.assertEqual(asked, [None, "video"])

    def test_the_rar_count_reads_the_named_lists_own_directory(self):
        self.build("video", self.video_dir, files=2, raw=GIB, size="1.00GB", rar=4)

        self.assertEqual(webserver.count_rar_album_folders("video"), 4)
        self.assertIsNone(webserver.count_rar_album_folders(),
                          "and the primary's directory is still where no name looks")


class OneBrokenListCostsItsOwnRowOnly(TwoLists):

    def test_a_list_that_cannot_be_read_leaves_the_other_in_the_totals(self):
        real = list_mod.get_file_count_date_size_and_raw_bytes

        def broken_for_video(name=None):
            if name == "video":
                raise OSError("an AV scanner is holding the list")
            return real(name)

        list_mod.get_file_count_date_size_and_raw_bytes = broken_for_video
        self.addCleanup(setattr, list_mod, "get_file_count_date_size_and_raw_bytes", real)

        library = webserver.build_library_payload()

        self.assertEqual(library["files"], 3)
        self.assertEqual(library["raw_bytes"], GIB + GIB // 2)
        self.assertEqual([row["files"] for row in library["lists"]], [3, 0])

    def test_a_lists_file_that_cannot_be_read_falls_back_to_the_one_primary(self):
        import library

        original = library.lists

        def boom():
            raise ValueError("lists.json is unreadable")

        library.lists = boom
        self.addCleanup(setattr, library, "lists", original)

        result = webserver.build_library_payload()

        self.assertEqual(result["files"], 3, "the primary, as the page always showed it")
        self.assertEqual(len(result["lists"]), 1)


class ASingleListInstallReadsAsBefore(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="dccore-statslib1-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.lists_dir = os.path.join(self.tmp, "lists")
        os.makedirs(self.lists_dir)
        self.set_config(LOCAL_LIST_DIR=self.lists_dir, NICKNAME="SomeBot",
                        LIST_BASE_NAME="SomeBot",
                        LISTS_FILE=os.path.join(self.tmp, "no-such-lists.json"))
        write(os.path.join(self.lists_dir, "SomeBot-2026-09-07.txt"),
              "List of 2 Files\n!SomeBot A.mp3  ::INFO:: 1.0MB\n!SomeBot B.mp3  ::INFO:: 1.0MB\n")
        # A stored size that is NOT what the bytes would format to: proves the
        # single-list path shows the list's own string, untouched.
        write(os.path.join(self.lists_dir, config.LIST_SIZE_FILE), "1.85TB")
        write(os.path.join(self.lists_dir, config.LIST_RAWBYTES_FILE), "4096")

    def test_the_totals_are_the_one_lists_own_figures(self):
        library = webserver.build_library_payload()

        self.assertEqual(library["files"], 2)
        self.assertEqual(library["size"], "1.85TB")
        self.assertEqual(library["raw_bytes"], 4096)
        self.assertEqual(len(library["lists"]), 1)

    def test_it_goes_through_the_stats_payload_the_same_way(self):
        self.assertEqual(webserver.build_stats_payload()["library"]["files"], 2)

    def test_a_bot_with_nothing_built_yet_says_so_rather_than_zero_albums(self):
        os.remove(os.path.join(self.lists_dir, "SomeBot-2026-09-07.txt"))

        library = webserver.build_library_payload()

        self.assertEqual(library["files"], 0)
        self.assertIsNone(library["rar_folders"])


def read_app_js():
    with io.open(os.path.join(REPO_ROOT, "web", "app.js"), encoding="utf-8") as handle:
        return handle.read()


class TheCardsAreBuiltNotFixed(unittest.TestCase):
    """How many Library cards there are depends on how many lists the bot
    serves, so the page builds them: four fixed cards had two of them empty or
    repeating each other on a bot with one list, and nowhere to say which list
    a figure belonged to on a bot with several."""

    def test_the_markup_has_one_container_and_no_fixed_library_cards(self):
        with io.open(os.path.join(REPO_ROOT, "web", "index.html"), encoding="utf-8") as handle:
            html = handle.read()
        self.assertIn('id="st-library"', html)
        for gone in ("st-files", "st-size", "st-albums", "st-built", "st-lists"):
            with self.subTest(id=gone):
                self.assertNotIn('id="%s"' % gone, html)

    def test_the_render_is_called_from_the_stats_render(self):
        code = read_app_js()
        self.assertIn("renderLibrary(lib);", code)

    def test_names_and_figures_are_written_as_text_not_markup(self):
        """A list's name is whatever the operator typed on the Library page."""
        code = read_app_js()
        start = code.index("function libraryCard(")
        body = code[start:code.index("\n  }\n", start)]
        self.assertIn("big.textContent = String(value);", body)
        self.assertIn("caption.textContent = String(label);", body)
        self.assertNotIn("innerHTML", body)

    def test_every_string_the_cards_use_exists_in_every_language(self):
        for code in ("en", "es", "fr"):
            with io.open(os.path.join(REPO_ROOT, "web", "lang", code + ".json"),
                         encoding="utf-8") as handle:
                strings = json.load(handle)
            for key in ("stats.labelledFileCount", "stats.albumFoldersCount",
                        "stats.listBuilt", "common.total"):
                with self.subTest(language=code, key=key):
                    self.assertTrue(strings.get(key), "%s.json has no %s" % (code, key))
            with self.subTest(language=code, gone="the By list strings"):
                self.assertNotIn("stats.byList", strings)
                self.assertNotIn("stats.listName", strings)


HARNESS = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[2], "utf8");
function fn(sig) {
  const s = src.indexOf(sig); if (s < 0) { throw new Error("saknas: " + sig); }
  let d = 0, i = src.indexOf("{", s);
  for (; i < src.length; i++) { if (src[i] === "{") { d++; } else if (src[i] === "}") { d--; if (d === 0) { break; } } }
  return src.slice(s, i + 1);
}
const code = ["function libraryCard(", "function libraryLabel(", "function renderLibrary("].map(fn).join("\n");
// toLocaleString() follows the machine's locale (a space here, a comma on the
// CI runners). What is under test is which numbers go where, not the locale,
// so it is pinned to one style.
Number.prototype.toLocaleString = function () { return String(this).replace(/\B(?=(\d{3})+(?!\d))/g, ","); };
const STRINGS = {
  "stats.labelledFileCount": "{label} \u00b7 {count} files",
  "stats.albumFoldersCount": "{count} album folders (!rar)",
  "stats.listBuilt": "List built", "common.total": "Total"
};
function element() {
  return { children: [], className: "", textContent: "",
    appendChild(c) { this.children.push(c); return c; },
    set innerHTML(v) { this.children = []; } };
}
const document = { createElement: element };
function run(lib) {
  const el = { stLibrary: element() };
  new Function("el", "document", "t", code + "\nrenderLibrary(" + JSON.stringify(lib) + ");")(el, document, k => STRINGS[k]);
  return el.stLibrary.children.map(card => ({ value: card.children[0].textContent, label: card.children[1].textContent, small: card.children[0].className.indexOf("stat-value-sm") >= 0 }));
}
const out = {};
out.one = run({ files: 64136, size: "1.85TB", rar_folders: 11322, list_date: "Sep 25th",
  lists: [{ name: "music", files: 64136, size: "1.85TB", rar_folders: 11322 }] });
out.two = run({ files: 71278, size: "20.44TB", rar_folders: 11322, list_date: "Sep 25th",
  lists: [{ name: "music", files: 64136, size: "1.85TB", rar_folders: 11322 },
          { name: "video", files: 7142, size: "18.59TB", rar_folders: null }] });
out.three = run({ files: 6, size: "3GB", rar_folders: null, list_date: null,
  lists: [{ name: "a", files: 1, size: "1GB", rar_folders: null }, { name: "<b>b</b>", files: 2, size: "1GB", rar_folders: 0 },
          { name: "c", files: 3, size: "1GB", rar_folders: null }] });
out.tricky = run({ files: 36, size: "3GB", rar_folders: null, list_date: null,
  lists: [{ name: "Rock $& Roll", files: 12, size: "1GB", rar_folders: null },
          { name: "Films $'", files: 12, size: "1GB", rar_folders: null },
          { name: "my {count} list", files: 12, size: "1GB", rar_folders: null }] });
out.none = run(null);
out.nolists = run({ files: 5, size: "1GB", rar_folders: 2, list_date: "Sep 1st" });
console.log(JSON.stringify(out));
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed; CI's runners have it")
class TheRealRenderBuildsTheCards(unittest.TestCase):

    def seen(self):
        handle, path = tempfile.mkstemp(suffix=".js")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(HARNESS)
            done = subprocess.run(["node", path, os.path.join(REPO_ROOT, "web", "app.js")],
                                  capture_output=True, timeout=60)
        finally:
            os.unlink(path)
        self.assertEqual(done.returncode, 0, done.stderr.decode("utf-8", "replace"))
        return json.loads(done.stdout.decode("utf-8"))

    def test_one_list_is_the_total_and_when_it_was_built_and_nothing_empty(self):
        cards = self.seen()["one"]

        self.assertEqual([card["label"] for card in cards],
                         ["Total \u00b7 64,136 files \u00b7 11,322 album folders (!rar)", "List built"])
        self.assertEqual(cards[0]["value"], "1.85TB")

    def test_two_lists_are_the_total_each_list_and_when_it_was_built(self):
        cards = self.seen()["two"]

        self.assertEqual([card["value"] for card in cards], ["20.44TB", "1.85TB", "18.59TB", "Sep 25th"])
        self.assertEqual(cards[0]["label"], "Total \u00b7 71,278 files \u00b7 11,322 album folders (!rar)")
        self.assertEqual(cards[1]["label"], "music \u00b7 64,136 files \u00b7 11,322 album folders (!rar)")

    def test_a_list_with_no_rar_list_says_nothing_about_albums(self):
        """Unknown is not zero, so it is left out rather than shown as 0."""
        self.assertEqual(self.seen()["two"][2]["label"], "video \u00b7 7,142 files")

    def test_a_list_with_a_rar_list_of_zero_says_zero(self):
        """Zero albums is a claim; no RAR list is not."""
        self.assertTrue(self.seen()["three"][2]["label"].endswith("2 files \u00b7 0 album folders (!rar)"))

    def test_the_number_of_cards_follows_the_number_of_lists(self):
        seen = self.seen()

        self.assertEqual(len(seen["one"]), 2)
        self.assertEqual(len(seen["two"]), 4)
        self.assertEqual(len(seen["three"]), 5)
        self.assertEqual(len(seen["nolists"]), 2, "an older payload with no lists still renders")
        self.assertEqual(len(seen["none"]), 2, "and so does no payload at all")

    def test_a_name_with_markup_is_text(self):
        self.assertTrue(self.seen()["three"][2]["label"].startswith("<b>b</b> \u00b7 "))

    def test_a_list_name_is_shown_as_typed_whatever_characters_it_has(self):
        """String.replace reads "$&" and "$'" in a replacement string as patterns,
        and a "{count}" in the name must not be filled in (#957 review)."""
        cards = self.seen()["tricky"]

        self.assertEqual([card["label"] for card in cards[1:4]],
                         ["Rock $& Roll \u00b7 12 files", "Films $' \u00b7 12 files",
                          "my {count} list \u00b7 12 files"])

    def test_an_unknown_build_date_is_a_dash_and_the_small_size(self):
        last = self.seen()["three"][-1]

        self.assertEqual(last["value"], "\u2014")
        self.assertTrue(last["small"], "a date is not a measurement")

    def test_the_list_built_card_is_always_last(self):
        for name, cards in self.seen().items():
            with self.subTest(case=name):
                self.assertEqual(cards[-1]["label"], "List built")


if __name__ == "__main__":
    unittest.main()
