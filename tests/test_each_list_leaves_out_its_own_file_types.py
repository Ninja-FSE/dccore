"""#1295: each served list can leave out file types of its own.

OmenServe has `Exclude = .mpu,.db` per list. LIST_IGNORED_EXTENSIONS does the
same for every list at once; a list's own `exclude` in data/lists.json adds
to it for that list only. No `exclude` - an old lists.json, a single-list
install - builds exactly as before.

The one part that is easy to get wrong is the save. The mIRC settings
window, a dashboard tab opened before the upgrade and the console's `served`
transaction all send the whole set of lists without the field, so a row
without it must KEEP the list's exclusions; only an explicit empty value
clears them.

Every nick, channel and folder here is invented.
"""

import io
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import library  # noqa: E402
import list as list_mod  # noqa: E402
import update_list  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402
from tests.test_console_settings_commands import ServedCase, as_input, read_bytes, run  # noqa: E402


class HowATypeIsWritten(unittest.TestCase):

    def test_case_dots_spaces_and_repeats_do_not_matter(self):
        self.assertEqual(library.normalise_extensions(" .DB, ini ,, .ini, Cue "), (".db", ".ini", ".cue"))
        self.assertEqual(library.normalise_extensions([".mpu", "DB"]), (".mpu", ".db"))
        self.assertEqual(library.normalise_extensions(None), ())
        self.assertEqual(library.normalise_extensions(""), ())

    def test_what_is_not_a_file_type_is_refused_on_save(self):
        for value, says in (("music/x", "no slashes"), ("a\\b", "no slashes"), ("my file", "spaces"),
                            ("\x0304db", "control"), (".", "not a file type"),
                            ("x" * (library.MAX_EXCLUSION_LENGTH + 1), "longer than")):
            with self.subTest(value=value):
                problems = library.extension_problems(value)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(says, problems[0])
        self.assertEqual(library.extension_problems(".db, ini"), [])

    def test_too_many_is_refused(self):
        many = ",".join(f".x{n}" for n in range(library.MAX_LIST_EXCLUSIONS + 1))
        self.assertIn("the most is", library.extension_problems(many)[-1])

    def test_a_hand_edited_bad_entry_is_dropped_on_load_not_the_rest(self):
        self.assertEqual(library.normalise_extensions(["db", "bad/one", "my file"]), (".db",))


class Stored(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.root = self.make_temp_dir()
        self.set_config(LISTS_FILE=os.path.join(self.root, "lists.json"))
        self.music = os.path.join(self.root, "Music")
        self.films = os.path.join(self.root, "Films")
        for path in (self.music, self.films):
            os.makedirs(path, exist_ok=True)

    def two_lists(self, music_exclude=(), films_exclude=()):
        return [library.ServedList("Music", True, ["#somemusic"], [library.Folder("Music", self.music)],
                                   {}, music_exclude),
                library.ServedList("Films", False, ["#somefilms"], [library.Folder("Films", self.films)],
                                   {}, films_exclude)]

    def stored_json(self):
        with io.open(library.lists_file(), encoding="utf-8") as handle:
            return json.load(handle)


class TheListsFile(Stored):

    def test_an_old_file_has_none(self):
        with io.open(library.lists_file(), "w", encoding="utf-8") as handle:
            json.dump([{"name": "Music", "primary": True, "channels": [], "folders": []}], handle)
        self.assertEqual(library.lists()[0].exclude, ())
        self.assertEqual(library.list_exclusions(), ())

    def test_saved_spelled_and_read_back(self):
        library.save_lists(self.two_lists(music_exclude="CUE, .Log"))
        self.assertEqual(self.stored_json()[0]["exclude"], [".cue", ".log"])
        self.assertNotIn("exclude", self.stored_json()[1], "a list without any writes none")
        self.assertEqual(library.list_exclusions("music"), (".cue", ".log"))
        self.assertEqual(library.list_exclusions(), (".cue", ".log"), "the primary, without a name")
        self.assertEqual(library.list_exclusions("Films"), ())
        self.assertEqual(library.list_exclusions("No such list"), ())

    def test_a_bad_one_refuses_the_save_and_names_the_list(self):
        with self.assertRaises(ValueError) as caught:
            library.save_lists(self.two_lists(films_exclude="my file"))
        self.assertIn("'Films': file types to leave out:", str(caught.exception))
        self.assertFalse(os.path.exists(library.lists_file()))


class TheBuild(Stored):
    """The real scan, one list at a time."""

    def setUp(self):
        super().setUp()
        tree = self.make_tree()
        self.set_config(LIST_BASE_NAME="alfa", NICKNAME="alfa", ORIGINAL_NICK="alfa",
                        LIST_IGNORED_EXTENSIONS=[".db"], SEPARATE_VIDEO_LIST=False)
        self.lists_dir = tree.lists
        for folder in (self.music, self.films):
            for name in ("track.flac", "notes.cue", "Thumbs.db"):
                with open(os.path.join(folder, name), "wb") as handle:
                    handle.write(b"\x00" * 2048)

    def listed(self, name):
        update_list.generate_master_list(name)
        with io.open(list_mod.find_latest_list(name), encoding="utf-8") as handle:
            return handle.read()

    def test_a_list_leaves_out_its_own_types_and_the_other_does_not(self):
        library.save_lists(self.two_lists(music_exclude=".cue"))

        music, films = self.listed("Music"), self.listed("Films")

        self.assertIn("track.flac", music)
        self.assertNotIn("notes.cue", music)
        self.assertIn("notes.cue", films)

    def test_the_global_setting_still_applies_to_both(self):
        library.save_lists(self.two_lists(music_exclude=".cue"))
        for name in ("Music", "Films"):
            with self.subTest(name=name):
                self.assertNotIn("Thumbs.db", self.listed(name))

    def test_none_set_builds_as_before(self):
        library.save_lists(self.two_lists())
        self.assertIn("notes.cue", self.listed("Music"))

    def test_the_set_is_the_global_one_then_the_lists_own(self):
        library.save_lists(self.two_lists(music_exclude="cue, db"))
        self.assertEqual(update_list.ignored_extensions("Music"), (".db", ".cue"))
        self.assertEqual(update_list.ignored_extensions(), (".db", ".cue"), "the primary")
        self.assertEqual(update_list.ignored_extensions("Films"), (".db",))


class TheDashboardsSave(Stored):

    def payload(self):
        return webserver.build_lists_payload()["lists"]

    def test_it_is_shown_only_where_there_is_one(self):
        library.save_lists(self.two_lists(music_exclude=".cue"))
        rows = self.payload()
        self.assertEqual(rows[0]["exclude"], [".cue"])
        self.assertNotIn("exclude", rows[1])

    def test_sent_as_typed_it_is_saved_spelled(self):
        library.save_lists(self.two_lists())
        rows = self.payload()
        rows[1]["exclude"] = "NFO, .sfv"
        status, _result = webserver.apply_list_changes({"lists": rows})
        self.assertEqual(status, 200)
        self.assertEqual(library.list_exclusions("Films"), (".nfo", ".sfv"))

    def test_a_row_without_the_field_keeps_what_the_list_has(self):
        """The mIRC window's save, or an old dashboard tab."""
        library.save_lists(self.two_lists(music_exclude=".cue", films_exclude=".nfo"))
        rows = self.payload()
        for row in rows:
            row.pop("exclude", None)
        rows[1]["channels"] = ["#somefilms", "#otherfilms"]

        status, _result = webserver.apply_list_changes({"lists": rows})

        self.assertEqual(status, 200)
        self.assertEqual(library.list_exclusions("Music"), (".cue",))
        self.assertEqual(library.list_exclusions("Films"), (".nfo",))

    def test_an_explicit_empty_value_clears_it(self):
        library.save_lists(self.two_lists(music_exclude=".cue"))
        rows = self.payload()
        rows[0]["exclude"] = ""
        self.assertEqual(webserver.apply_list_changes({"lists": rows})[0], 200)
        self.assertEqual(library.list_exclusions("Music"), ())

    def test_a_bad_value_is_refused_with_the_reason(self):
        library.save_lists(self.two_lists())
        rows = self.payload()
        rows[0]["exclude"] = "my file"
        status, result = webserver.apply_list_changes({"lists": rows})
        self.assertEqual(status, 400)
        self.assertIn("file types to leave out", result["error"])
        # A number would otherwise read as the type ".5".
        rows[0]["exclude"] = 5
        status, result = webserver.apply_list_changes({"lists": rows})
        self.assertEqual(status, 400)
        self.assertIn("'exclude' must be a list", result["error"])


class TheConsoleTransaction(ServedCase):
    """`served` begin/list/chan/folder/commit carries no exclusions, which is
    what the mIRC settings window sends."""

    def setUp(self):
        super().setUp()
        self.set_config(LISTS_FILE=os.path.join(self.root, "lists.json"))
        library.save_lists([
            library.ServedList("Main", True, ["#music"], [library.Folder("Music", self.music)], {}, ".cue"),
            library.ServedList("Rock", False, ["#rock"], [library.Folder("Rock", self.rock)], {}, ".nfo, .sfv"),
        ])

    def send_back(self, edit=None):
        lines = run(self.session, "served")
        run(self.session, "served begin")
        for line in as_input(lines):
            if edit:
                line = edit(line)
            run(self.session, line)
        return run(self.session, "served commit")

    def test_sent_back_unchanged_it_is_still_unchanged(self):
        before = read_bytes(library.lists_file())
        self.assertEqual(self.send_back(), ["DCCORE SRVDONE unchanged Nothing changed; nothing was saved."])
        self.assertEqual(read_bytes(library.lists_file()), before)

    def test_a_change_saved_from_it_keeps_every_lists_exclusions(self):
        said = self.send_back(lambda line: line.replace("#rock normal", "#rock quiet"))
        self.assertTrue(said[-1].startswith("DCCORE SRVDONE ok 2"), said)
        self.assertEqual(library.list_exclusions("Main"), (".cue",))
        self.assertEqual(library.list_exclusions("Rock"), (".nfo", ".sfv"))

    def test_the_typed_snapshot_shows_them(self):
        session = self.session
        session.structured = False
        text = "\n".join(run(session, "served"))
        self.assertIn("leaves out: .nfo, .sfv", text)


if __name__ == "__main__":
    unittest.main()
