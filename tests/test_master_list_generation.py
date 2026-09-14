"""generate_master_list() - the scan that produces the file every user downloads.

update_list.py was the last module with no test of its scan path. The stats it
publishes alongside the list are covered by test_list_stats.py; this is the list
itself: what the scan picks up, what the two files contain, that the header the
advert reads agrees with the lines the search reads, and that a failed run leaves
the previous index untouched.

It also pins an invariant that is currently load-bearing and undocumented - see
TheRequestTriggerIsStable.
"""

import contextlib
import io
import os
import sys
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
if os.path.join(REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(REPO_ROOT, "tests"))

import commands  # noqa: E402
import defaults as config  # noqa: E402
import irc  # noqa: E402
import library  # noqa: E402
import list as list_mod  # noqa: E402
import platform_compat  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class MasterListCase(DCCoreTestCase):
    """A library on disk, and a place to write the lists."""

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        config.LOCAL_LIST_DIR = self.tree.lists
        config.FILE_DIRECTORY = self.tree.music
        config.LIST_BASE_NAME = "DCCore"
        config.NICKNAME = "DCCore"
        config.ORIGINAL_NICK = "DCCore"

    def add(self, relative, data=b"\x00" * 2048):
        path = os.path.join(self.tree.music, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def use_empty_library(self):
        """Start from nothing instead of TempTree's two baseline .flac tracks.

        For the tests that assert exact counts. Doing the arithmetic against a
        fixture this file does not own would break the moment somebody adds a
        third baseline track, and the failure would read as a bug in the
        counting rather than in the sum."""
        empty = os.path.join(self.tree.root, "empty-library")
        os.makedirs(empty, exist_ok=True)
        config.FILE_DIRECTORY = empty
        self.tree.music = empty

    def generate(self):
        return update_list.generate_master_list()

    def list_path(self):
        # The film list is excluded here for the same reason
        # list.find_latest_list() excludes it: it is named
        # "<base>-VIDEO-<date>.txt", so it matches this prefix too. This
        # helper missed it at first and every video test failed with "expected
        # one master list, found 2" - the same shape as the production bug,
        # found by the same omission.
        names = [n for n in os.listdir(self.tree.lists)
                 if n.startswith(config.LIST_BASE_NAME)
                 and n.endswith(".txt") and "-RAR-" not in n
                 and f"-{list_mod.VIDEO_LIST_MARKER}-" not in n]
        self.assertEqual(len(names), 1, f"expected one master list, found {names}")
        return os.path.join(self.tree.lists, names[0])

    def rar_path(self):
        names = [n for n in os.listdir(self.tree.lists) if "-RAR-" in n]
        self.assertEqual(len(names), 1, f"expected one album list, found {names}")
        return os.path.join(self.tree.lists, names[0])

    def read_list(self):
        """The MUSIC list only. Film and series have their own file."""
        with open(self.list_path(), encoding="utf-8") as handle:
            return handle.read()

    def video_list_path(self):
        names = [n for n in os.listdir(self.tree.lists)
                 if n.startswith(config.LIST_BASE_NAME)
                 and f"-{list_mod.VIDEO_LIST_MARKER}-" in n and n.endswith(".txt")]
        self.assertEqual(len(names), 1,
                         f"expected one film list, found {names}")
        return os.path.join(self.tree.lists, names[0])

    def read_video_list(self):
        with open(self.video_list_path(), encoding="utf-8") as handle:
            return handle.read()

    def has_video_list(self):
        """False is the ordinary case: the file is only published when there
        is video to put in it."""
        return any(f"-{list_mod.VIDEO_LIST_MARKER}-" in n
                   for n in os.listdir(self.tree.lists))

    def read_everything(self):
        """Both lists, for the many tests whose question is "is this file
        indexed at all" rather than "which list did it land in"."""
        text = self.read_list()
        if self.has_video_list():
            text += self.read_video_list()
        return text

    def request_lines(self):
        """Request rows across every list this scan published."""
        return [line for line in self.read_everything().split("\n")
                if line.startswith("!")]

    def music_request_lines(self):
        return [line for line in self.read_list().split("\n")
                if line.startswith("!")]


class ScanFindsTheRightFiles(MasterListCase):
    """TempTree starts with two .flac tracks in one album folder."""

    def test_the_baseline_library_is_listed(self):
        self.assertTrue(self.generate())
        self.assertEqual(len(self.request_lines()), 2)

    def test_mp3_and_flac_are_both_picked_up(self):
        self.add("Artist/Album/track.mp3")
        self.assertTrue(self.generate())
        names = self.read_list()
        self.assertIn("track.mp3", names)
        self.assertIn("Enter Sandman.flac", names)

    def test_the_companions_of_a_music_library_are_listed_too(self):
        """This asserted the opposite until the rule changed, and the reversal
        is the point of the change: covers, cue sheets, playlists and notes
        used to be invisible because they were not .mp3 or .flac.

        People do serve them. An operator who would rather not can name them
        in LIST_IGNORED_EXTENSIONS; nothing has to guess on their behalf.
        """
        for companion in ("Artist/Album/cover.jpg", "Artist/Album/info.nfo",
                          "Artist/Album/playlist.m3u", "Artist/Album/notes.txt",
                          "Artist/Album/disc.cue"):
            self.add(companion, b"data")

        self.assertTrue(self.generate())

        listing = self.read_list()
        for companion in ("cover.jpg", "info.nfo", "playlist.m3u",
                          "notes.txt", "disc.cue"):
            with self.subTest(file=companion):
                self.assertIn(companion, listing)
        self.assertEqual(len(self.request_lines()), 7)

    def test_the_extension_test_is_case_insensitive(self):
        """Ripped libraries are full of .FLAC and .Mp3."""
        self.add("Artist/Loud/SHOUTING.FLAC")
        self.add("Artist/Loud/Mixed.Mp3")
        self.assertTrue(self.generate())
        self.assertEqual(len(self.request_lines()), 4)

    def test_it_recurses_all_the_way_down(self):
        self.add("A/B/C/D/E/deep.flac")
        self.assertTrue(self.generate())
        self.assertIn("deep.flac", self.read_list())

    def test_an_empty_library_still_produces_a_list(self):
        empty = os.path.join(self.tree.root, "empty-library")
        os.makedirs(empty, exist_ok=True)
        config.FILE_DIRECTORY = empty
        self.assertTrue(self.generate())
        self.assertEqual(self.request_lines(), [])


class EveryFileIsListedUnlessItIsNamed(MasterListCase):
    """The scan used to be a hardcoded `('.mp3', '.flac')`.

    So a video library - or an .m4a one, or anything else - walked past every
    file it owned and published nothing, silently: the scan reported success,
    the list was built, and it was empty. That reads as "the bot cannot see my
    files" with nothing saying why.

    Naming what to KEEP could never fix that, only move it. The set of things
    people serve is open-ended, so every format left out of the list is
    invisible in exactly the same way. Naming what to SKIP is a short, closed
    list, and getting it wrong costs a file listed that need not have been -
    not a library that does not appear.
    """

    def test_a_video_library_is_listed(self):
        """The defect, stated as the case it failed on."""
        self.add("Films/Feature/Feature.mkv")
        self.add("Films/Feature/Extras.mp4")

        self.assertTrue(self.generate())

        listing = self.read_everything()
        self.assertIn("Feature.mkv", listing)
        self.assertIn("Extras.mp4", listing)

    def test_a_format_nobody_thought_of_is_listed(self):
        """The property an include-list cannot have. These are not in any
        default anywhere, and that is the point: nothing had to predict them."""
        for name in ("Films/Odd/lecture.m4b", "Films/Odd/tape.ape",
                     "Films/Odd/scan.djvu", "Films/Odd/game.chd"):
            self.add(name)

        self.assertTrue(self.generate())

        listing = self.read_list()
        for name in ("lecture.m4b", "tape.ape", "scan.djvu", "game.chd"):
            with self.subTest(file=name):
                self.assertIn(name, listing)

    def test_a_file_with_no_extension_at_all_is_listed(self):
        """"Every file" includes the ones with nothing to match on."""
        self.add("Films/Odd/README")

        self.assertTrue(self.generate())

        self.assertIn("README", self.read_list())

    def test_a_named_extension_is_skipped(self):
        self.set_config(LIST_IGNORED_EXTENSIONS=[".jpg", ".nfo"])
        self.add("Films/Feature/Feature.mkv")
        self.add("Films/Feature/poster.jpg", b"junk")
        self.add("Films/Feature/info.nfo", b"junk")

        self.assertTrue(self.generate())

        listing = self.read_everything()
        self.assertIn("Feature.mkv", listing)
        self.assertNotIn("poster.jpg", listing)
        self.assertNotIn("info.nfo", listing)

    def test_the_shipped_default_skips_the_droppings_and_nothing_else(self):
        """Derived from the setting, not a second list of names here. The
        default is deliberately narrow - only what is never a served file."""
        for ext in config.SHIPPED_VALUES["LIST_IGNORED_EXTENSIONS"]:
            self.add("Films/Junk/leftover" + ext, b"junk")
        self.add("Films/Junk/cover.jpg")
        self.add("Films/Junk/Feature.mkv")

        self.assertTrue(self.generate())

        listing = self.read_everything()
        for ext in config.SHIPPED_VALUES["LIST_IGNORED_EXTENSIONS"]:
            with self.subTest(extension=ext):
                self.assertNotIn("leftover" + ext, listing)
        self.assertIn("cover.jpg", listing)
        self.assertIn("Feature.mkv", listing)

    def test_a_half_finished_download_is_not_offered(self):
        """The one case where listing a file is actively wrong: the bytes are
        not all there, so the transfer can only ever hand over a broken file."""
        self.add("Films/Feature/Feature.mkv.part", b"half")

        self.assertTrue(self.generate())

        self.assertNotIn("Feature.mkv.part", self.read_list())

    def test_an_empty_setting_skips_nothing(self):
        """A perfectly good answer under this model, and the reason it needs
        no fallback: an empty INCLUDE list scanned a library to zero files and
        had to guess its way out. An empty EXCLUDE list just lists the
        library."""
        self.set_config(LIST_IGNORED_EXTENSIONS=[])
        self.add("Films/Junk/Thumbs.db", b"junk")
        self.add("Films/Feature/Feature.mkv")

        self.assertTrue(self.generate())

        listing = self.read_everything()
        self.assertIn("Thumbs.db", listing)
        self.assertIn("Feature.mkv", listing)

    def test_dots_are_optional_and_spacing_does_not_matter(self):
        """What a person actually types. The setting is reached from
        settings.conf, admin_config.py and the dashboard's Settings page, and
        only one of those goes near a validator - so every form of "db, .ini,
        tmp" has to mean the same thing."""
        self.set_config(LIST_IGNORED_EXTENSIONS=["db", " .INI ", "tmp"])
        self.add("Films/Junk/Thumbs.db", b"junk")
        self.add("Films/Junk/desktop.ini", b"junk")
        self.add("Films/Junk/half.TMP", b"junk")
        self.add("Films/Feature/Feature.mkv")

        self.assertTrue(self.generate())

        listing = self.read_everything()
        for skipped in ("Thumbs.db", "desktop.ini", "half.TMP"):
            with self.subTest(file=skipped):
                self.assertNotIn(skipped, listing)
        self.assertIn("Feature.mkv", listing)

    def test_the_dot_is_what_makes_it_an_extension_and_not_a_suffix(self):
        """The reason normalisation adds the dot, which is NOT "so the file
        matches" - `"Thumbs.db".endswith("db")` is already true, and a
        mutation dropping the dot passed a test that only checked that.

        It is the dot that stops an extension matching the END OF A NAME.
        Under an exclude-list the cost of getting this wrong is worse than it
        was under an include-list: a file that merely ENDS in those letters
        disappears from the library with nothing said.
        """
        self.set_config(LIST_IGNORED_EXTENSIONS=["ts"])
        self.add("Films/Feature/Episode.ts", b"junk")
        self.add("Films/Feature/credits")
        self.add("Films/Feature/highlights")

        self.assertTrue(self.generate())

        listing = self.read_list()
        self.assertNotIn("Episode.ts", listing)
        self.assertIn("credits", listing)
        self.assertIn("highlights", listing)

    def test_a_video_row_is_written_exactly_like_an_audio_one(self):
        """One line, the request trigger, the name, then the ::INFO:: size -
        no second form for a second kind of file. list.py splits on the marker
        regardless of extension, and so do the OmenServe bots the convention
        came from."""
        self.add("Films/Feature/Feature.mkv", b"\x00" * 4096)

        self.assertTrue(self.generate())

        rows = [line for line in self.request_lines() if "Feature.mkv" in line]
        self.assertEqual(len(rows), 1, "a video file produced other than one row")
        self.assertTrue(rows[0].startswith("!" + config.NICKNAME + " "))
        self.assertIn("  ::INFO:: ", rows[0])

    def test_a_video_file_counts_towards_the_advertised_total(self):
        """The header total and the advert both read the list back. A file
        indexed but uncounted would advertise a number the list disagrees
        with."""
        self.add("Films/Feature/Feature.mkv")

        self.assertTrue(self.generate())

        count = list_mod.get_file_count_date_size_and_raw_bytes()[0]
        self.assertEqual(count, len(self.request_lines()))


class TheSettingIsResolvedOncePerScan(MasterListCase):
    """Not once per file.

    is_listed_file() reads the setting, normalises it and builds a tuple.
    Asked per file, a 719k-file library - the largest this project has
    measured - does that 719,000 times.
    """

    def test_it_is_read_once_however_many_files_there_are(self):
        real = update_list.ignored_extensions
        calls = []

        def counting():
            calls.append(1)
            return real()

        update_list.ignored_extensions = counting
        self.addCleanup(setattr, update_list, "ignored_extensions", real)

        for i in range(12):
            self.add(f"Films/Many/clip{i}.mkv")

        self.assertTrue(self.generate())

        self.assertIn("clip11.mkv", self.read_everything())
        self.assertLessEqual(len(calls), 2,
                             f"the setting was read {len(calls)} times for one "
                             f"scan of 14 files")

    def test_the_scan_says_what_it_is_skipping(self):
        """The reported symptom was "my files are not in the list" with
        nothing anywhere saying why. This line is the answer to it, so it is
        worth a test rather than being decoration."""
        self.set_config(LIST_IGNORED_EXTENSIONS=[".db", ".ini"])
        self.add("Films/Feature/Feature.mkv")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertTrue(self.generate())

        output = buffer.getvalue()
        self.assertIn("Indexing every file", output)
        self.assertIn(".db", output)
        self.assertIn(".ini", output)

    def test_it_says_so_when_nothing_is_skipped(self):
        """An empty setting is a real configuration, not a broken one, and the
        log should not read as though the line failed to render."""
        self.set_config(LIST_IGNORED_EXTENSIONS=[])
        self.add("Films/Feature/Feature.mkv")

        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertTrue(self.generate())

        self.assertIn("no extensions are being skipped", buffer.getvalue())


class EditingItAndRehashingIsTheWholeWorkflow(MasterListCase):
    """The operator's actual sequence: name a format, !rehash, it disappears.

    This covers the reload half: the value in settings.conf reaches config and
    the scan sees it. The other half - that a rehash does not PRESERVE the old
    value over the top - lives where that list does, in
    test_commands.RehashPreservesEveryRuntimeContainer. Split that way because
    PRESERVE_RUNTIME is applied by the rehash handler rather than by
    reload_modules_in_order(), so this test never reaches it; a mutation
    adding the setting to PRESERVE_RUNTIME passed here.
    """

    def test_a_rehash_picks_up_a_newly_ignored_format(self):
        path = os.path.join(self.tree.root, "settings.conf")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("LIST_IGNORED_EXTENSIONS = jpg\n")

        real = os.environ.get("DCCORE_SETTINGS_FILE")
        os.environ["DCCORE_SETTINGS_FILE"] = path
        self.addCleanup(
            lambda: os.environ.__setitem__("DCCORE_SETTINGS_FILE", real)
            if real is not None else os.environ.pop("DCCORE_SETTINGS_FILE", None))

        self.set_config(LIST_IGNORED_EXTENSIONS=[])
        self.assertTrue(update_list.is_listed_file("cover.jpg"))

        with contextlib.redirect_stdout(io.StringIO()):
            commands.reload_modules_in_order(modules=("defaults",),
                                             reload_self=False)

        self.assertFalse(update_list.is_listed_file("cover.jpg"),
                         "the rehash did not pick up the edited setting")


class TheWayAnOperatorTypesIt(DCCoreTestCase):
    """ignored_extensions(), directly - every spelling of the same answer.

    Asked for explicitly: the setting must accept the extensions with or
    without a leading dot, and with or without a space after the comma.
    settings.conf arrives already split on commas by settings_file.coerce();
    admin_config.py may hand over a list or a bare string.
    """

    def extensions(self, value):
        self.set_config(LIST_IGNORED_EXTENSIONS=value)
        return update_list.ignored_extensions()

    def test_with_dots_and_without_are_the_same(self):
        self.assertEqual(self.extensions(["db", ".ini", "tmp"]),
                         (".db", ".ini", ".tmp"))

    def test_spacing_around_the_comma_does_not_matter(self):
        """The three ways a person writes one list."""
        for written in ("db,ini,tmp", "db, ini, tmp", " db ,  .ini ,tmp  "):
            with self.subTest(typed=written):
                self.assertEqual(self.extensions(written),
                                 (".db", ".ini", ".tmp"))

    def test_case_does_not_matter(self):
        self.assertEqual(self.extensions([".DB", "INI"]), (".db", ".ini"))

    def test_a_list_and_a_string_mean_the_same_thing(self):
        """settings.conf gives a list (coerce() splits it); admin_config.py is
        plain Python and an operator may well write the comma-separated form
        there, having just seen it in settings.conf."""
        self.assertEqual(self.extensions("db, .ini"),
                         self.extensions([" db", ".INI "]))

    def test_blanks_and_duplicates_are_dropped(self):
        self.assertEqual(self.extensions(["db", "", "  ", ".DB", "db"]),
                         (".db",))

    def test_an_empty_setting_is_an_empty_answer(self):
        """No fallback: skipping nothing is what an operator asking for
        nothing to be skipped should get."""
        self.assertEqual(self.extensions([]), ())
        self.assertEqual(self.extensions(""), ())


class FilmAndSeriesGetTheirOwnList(MasterListCase):
    """One walk, two lists, both in the archive @<botnick> already sends.

    The pattern is not new here: the !rar album list has been a separate file
    built from the same walk since long before this. What is new is that the
    master list stopped being the only place a request can be answered from,
    which is the whole risk of the change - see StillRequestableAfterTheSplit.
    """

    def test_a_film_goes_in_the_film_list_and_not_the_music_one(self):
        self.add("Films/Some Film (2021)/Some Film.mkv")
        self.add("Music/Artist/Album/Track.flac")

        self.assertTrue(self.generate())

        self.assertIn("Some Film.mkv", self.read_video_list())
        self.assertNotIn("Some Film.mkv", self.read_list())
        self.assertIn("Track.flac", self.read_list())
        self.assertNotIn("Track.flac", self.read_video_list())

    def test_one_folder_holding_both_is_split_between_them(self):
        """The case the review names. A folder does not have to pick."""
        self.add("Mixed/Concert/Live Set.flac")
        self.add("Mixed/Concert/Live Set.mkv")

        self.assertTrue(self.generate())

        self.assertIn("Live Set.flac", self.read_list())
        self.assertNotIn("Live Set.mkv", self.read_list())
        self.assertIn("Live Set.mkv", self.read_video_list())
        self.assertNotIn("Live Set.flac", self.read_video_list())

    def test_everything_else_stays_with_the_music(self):
        """Artwork, cue sheets and notes sit beside the tracks they belong to.
        A film's subtitles land there too - the one rough edge of deciding per
        file, and deliberate: see LIST_VIDEO_EXTENSIONS."""
        for name in ("Music/Artist/Album/cover.jpg", "Music/Artist/Album/disc.cue",
                     "Music/Artist/Album/notes.txt", "Music/Artist/Album/README"):
            self.add(name, b"data")

        self.assertTrue(self.generate())

        listing = self.read_list()
        for name in ("cover.jpg", "disc.cue", "notes.txt", "README"):
            with self.subTest(file=name):
                self.assertIn(name, listing)
        self.assertFalse(self.has_video_list())

    def test_a_music_only_library_gets_no_film_list_at_all(self):
        """Not an empty file. Most libraries this serves are music only, and
        an empty extra member in every download is clutter with no use."""
        self.add("Music/Artist/Album/Track.flac")

        self.assertTrue(self.generate())

        self.assertFalse(self.has_video_list(),
                         "a film list was published for a library with no film")

    def test_both_lists_travel_in_the_archive_the_bot_hands_out(self):
        """No new trigger and nothing to learn: "@<botnick>" already sends one
        archive holding the master list and the album list."""
        self.add("Films/Feature/Feature.mkv")
        self.add("Music/Artist/Album/Track.flac")

        self.assertTrue(self.generate())

        names = [n for n in os.listdir(self.tree.lists) if n.endswith(".zip")]
        self.assertEqual(len(names), 1, f"expected one archive, found {names}")
        with zipfile.ZipFile(os.path.join(self.tree.lists, names[0])) as archive:
            members = archive.namelist()

        self.assertIn(os.path.basename(self.list_path()), members)
        self.assertIn(os.path.basename(self.video_list_path()), members)

    def test_a_rebuild_that_finds_no_film_drops_the_previous_film_list(self):
        """SAME DAY, which is the whole difference.

        The keep set asked whether a file existed at video_path - and that
        path carries today's date, so on a second rebuild it is the EARLIER
        run's output. A run that found no film kept it: a film list naming
        films that are gone, still searchable, still counted by the advert,
        and absent from the archive users actually download.

        It self-corrects across a date boundary, which is exactly why a
        single-build test never saw it. Found by audit.
        """
        self.use_empty_library()
        self.add("Music/Artist/Album/Track.flac")
        self.add("Films/Feature/Feature.mkv")
        self.assertTrue(self.generate())
        self.assertTrue(self.has_video_list())

        os.remove(os.path.join(self.tree.music, "Films", "Feature", "Feature.mkv"))
        self.assertTrue(self.generate())

        self.assertFalse(self.has_video_list(),
                         "a film list from an earlier run the same day "
                         "survived a rebuild that found no film")
        count = list_mod.get_file_count_date_size_and_raw_bytes()[0]
        self.assertEqual(count, 1, "the advert still counts the deleted film")
        _entries, total = list_mod.find_matching_entries(["feature"])
        self.assertEqual(total, 0, "the deleted film is still findable")

    def test_the_film_list_survives_the_prune_that_follows_it(self):
        """It is named "<base>-VIDEO-<date>.txt", so _prune_superseded_lists()
        sees it as a generated list and would remove it - published and
        deleted in the same run, every run, with only a "[LIST-CLEAN] Removed
        1 superseded list(s)" line to show for it. That reads like
        housekeeping working, which is exactly how the size side files were
        lost once before."""
        self.add("Films/Feature/Feature.mkv")

        self.assertTrue(self.generate())

        self.assertTrue(self.has_video_list(),
                        "the film list was published and then pruned away")

    def test_the_switch_turns_it_off(self):
        """Off is a real answer. An operator whose films and music are already
        in separate folders gets two lists from the multi-list feature
        instead, keyed on the folders that already carry the answer."""
        self.set_config(SEPARATE_VIDEO_LIST=False)
        self.add("Films/Feature/Feature.mkv")
        self.add("Music/Artist/Album/Track.flac")

        self.assertTrue(self.generate())

        self.assertFalse(self.has_video_list())
        listing = self.read_list()
        self.assertIn("Feature.mkv", listing)
        self.assertIn("Track.flac", listing)

    def test_the_header_describes_the_list_it_is_on(self):
        """The music list's size used to be the whole library's, films
        included - a number a reader cannot reconcile with the file in front
        of them."""
        self.use_empty_library()
        self.add("Films/Feature/Feature.mkv", b"\x00" * 40960)
        self.add("Music/Artist/Album/Track.flac", b"\x00" * 1024)

        self.assertTrue(self.generate())

        # Parenthesised, because "1.00KB" is a substring of "41.00KB" -
        # the combined size this test exists to reject. The first version
        # asserted the bare number and passed against exactly that.
        music_header = self.read_list().split("\n")[0]
        self.assertIn("1 Files", music_header)
        self.assertIn("(1.00KB)", music_header)

        video_header = self.read_video_list().split("\n")[0]
        self.assertIn("1 Films & Series", video_header)

    def test_a_library_of_nothing_but_film_still_publishes(self):
        """The zero-files guard counts the MUSIC list, so an all-film library
        looks empty to it.

        REBUILT, not built once: on a first run the guard finds no previous
        index and publishes anyway, so the defect is invisible. It is the
        second run that fails - "scan found 0 files but an index already
        exists (mount unavailable?)" - which means an operator serving only
        film gets one working list and then every !update refused for ever,
        blaming a mount that is fine. A mutation removing the fix passed a
        single-run version of this test.
        """
        self.use_empty_library()
        self.add("Films/A Film/A Film.mkv")
        self.add("Films/Another/Another.mp4")
        self.assertTrue(self.generate(), "an all-film library refused to publish")

        self.assertTrue(self.generate(),
                        "an all-film library refused to REBUILD once it had "
                        "an index of its own")

        self.assertIn("A Film.mkv", self.read_video_list())


class TheOperatorsOwnNameDoesNotHideTheirList(MasterListCase):
    """LIST_BASE_NAME is whatever the operator's nickname makes it, and the
    builder's markers sit AFTER it. Testing the whole path for them let a
    perfectly ordinary name decide this bot had no list at all."""

    def test_a_base_name_containing_the_film_marker_still_finds_the_master(self):
        """`-VIDEO-` in the name excluded the master list from its own search:
        @find answered "No MasterList found" and the advert published 0 files,
        permanently, with the list sitting right there in the directory."""
        config.LIST_BASE_NAME = "Bot-VIDEO-Archive"
        self.add(os.path.join("Artist", "Album", "01 - Track.flac"))
        self.assertTrue(self.generate())

        found = list_mod.find_latest_list()

        self.assertIsNotNone(found, "the master list was excluded by its own name")
        self.assertNotIn(f"-{list_mod.VIDEO_LIST_MARKER}-",
                         os.path.basename(found)[len(config.LIST_BASE_NAME):])

    def test_and_one_containing_the_album_marker_does_too(self):
        config.LIST_BASE_NAME = "RAR-RADIO-RAR"
        self.add(os.path.join("Artist", "Album", "01 - Track.flac"))
        self.assertTrue(self.generate())

        self.assertIsNotNone(list_mod.find_latest_list())

    def test_the_film_list_is_still_kept_out_when_the_name_is_ordinary(self):
        """The guard this replaces was doing a real job - a mutation that
        simply deleted it has to fail."""
        self.add(os.path.join("Films", "Some Film (2019).mkv"))
        self.add(os.path.join("Artist", "Album", "01 - Track.flac"))
        self.assertTrue(self.generate())

        found = list_mod.find_latest_list()

        self.assertNotIn(f"-{list_mod.VIDEO_LIST_MARKER}-", os.path.basename(found))


class WhatAKilledRunLeavesBehind(MasterListCase):
    """A run that FAILS discards its own staging files. A run that is killed -
    the machine goes down mid-scan - cannot."""

    def leftovers(self):
        return sorted(n for n in os.listdir(self.tree.lists) if n.endswith(".new"))

    def test_the_next_build_sweeps_them(self):
        """They carry the date they were staged on, so the next day's run
        stages different names and never touches them again: one set per
        killed run, for ever, in the directory the operator looks at to see
        whether their lists are being built."""
        for name in ("DCCore-2020-01-01.txt.new", "DCCore-RAR-2020-01-01.txt.new",
                     "DCCore-VIDEO-2020-01-01.txt.new", "DCCore-2020-01-01.zip.new"):
            with open(os.path.join(self.tree.lists, name), "w") as handle:
                handle.write("half a scan")

        self.add(os.path.join("Artist", "Album", "01 - Track.flac"))
        self.assertTrue(self.generate())

        self.assertEqual(self.leftovers(), [])

    def test_it_leaves_everything_that_is_not_its_own_staging_file(self):
        """Only names the builder itself stages, in the lists directory."""
        # No real list among these: an old "DCCore-<date>.txt" IS superseded
        # and _prune_superseded_lists removes it at the end of the build, by
        # design. This is about the sweep not reaching past its own names.
        keep = ("notes.new", "Someone-Else-2020-01-01.txt.new", "not-mine.zip.new")
        for name in keep:
            with open(os.path.join(self.tree.lists, name), "w") as handle:
                handle.write("not mine")

        self.add(os.path.join("Artist", "Album", "01 - Track.flac"))
        self.assertTrue(self.generate())

        for name in keep:
            with self.subTest(name=name):
                self.assertTrue(
                    os.path.exists(os.path.join(self.tree.lists, name)),
                    f"{name} was removed and does not belong to this builder")

    def test_the_sweep_says_what_it_removed(self):
        """Silent housekeeping is how the size side files were lost once
        already - the log line read like it was working."""
        with open(os.path.join(self.tree.lists, "DCCore-2020-01-01.txt.new"), "w") as handle:
            handle.write("half a scan")

        self.add(os.path.join("Artist", "Album", "01 - Track.flac"))
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.generate()

        self.assertIn("interrupted run", buffer.getvalue())


class StillRequestableAfterTheSplit(MasterListCase):
    """The regression the split could so easily have been.

    A file request, an @find and the advert count all read find_latest_list()
    alone. Move film and series into a second file without touching those,
    and every video in the library is listed, advertised and impossible to
    get - a feature that reads as working right up until somebody asks for
    something.
    """

    def setUp(self):
        super().setUp()
        self.use_empty_library()
        self.add("Films/Some Film (2021)/Some Film.mkv")
        self.add("Music/Artist/Album/Track.flac")
        self.assertTrue(self.generate())

    def test_find_latest_list_still_means_the_music_list(self):
        """It globs "<base>-*.txt" and takes the LAST. "-VIDEO-" sorts after
        the bare date, so without a guard the film list wins and becomes the
        list @find searches and the advert counts."""
        self.assertEqual(os.path.basename(list_mod.find_latest_list()),
                         os.path.basename(self.list_path()))

    def test_every_list_is_offered_to_a_request(self):
        paths = [os.path.basename(p) for p in list_mod.all_list_paths()]

        self.assertEqual(paths, [os.path.basename(self.list_path()),
                                 os.path.basename(self.video_list_path())])

    def test_a_film_is_still_findable(self):
        _entries, total = list_mod.find_matching_entries(["some", "film"])

        self.assertEqual(total, 1, "@find can no longer see films")

    def test_a_track_is_still_findable(self):
        _entries, total = list_mod.find_matching_entries(["track"])

        self.assertEqual(total, 1)

    def test_one_search_spans_both_lists(self):
        """A term matching in each returns both, counted together - the
        header reports the true total rather than one file's worth."""
        entries, total = list_mod.find_matching_entries([])

        self.assertEqual(total, 2)
        self.assertEqual({e["filename"] for e in entries},
                         {"Some Film.mkv", "Track.flac"})

    def test_the_advert_counts_every_list(self):
        """The bot serves both, so the number it announces covers both -
        otherwise it advertises a library smaller than the one in the archive
        it hands out."""
        count, _date, _size, _raw = \
            list_mod.get_file_count_date_size_and_raw_bytes()

        self.assertEqual(count, 2)

    def test_the_request_lookup_reads_every_list(self):
        """dcc.py turns a bare "!<nick> Some Film.mkv" into a path by scanning
        the list. Read out of the source: driving the real resolution needs a
        socket and a peer, and the one line that matters is which lists it
        opens."""
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as fh:
            code = "\n".join(line.split("#", 1)[0]
                              for line in fh.read().splitlines())

        self.assertIn("list_mod.all_list_paths(", code,
                      "dcc.py resolves requests against one list file, so "
                      "every film in the library is listed and un-downloadable")
        # The ARGUMENT is #26's half of the same question: all_list_paths()
        # reads every FILE of one list, and this says which list. Asserted as
        # one sequence, because reading every file of the wrong list is its
        # own way of answering a request with somebody else's library.
        self.assertIn("list_mod.all_list_paths(wanted_list)", code,
                      "dcc.py reads the primary's lists whatever channel the "
                      "request arrived in")


class OnlyWhatIsWorthPackingIsPackable(MasterListCase):
    """A folder earned its !rar row from holding ANY indexed file.

    That read as "album folders" while the scan took .mp3 and .flac and
    nothing else. The moment it took every file, every folder in the library
    became packable - a season of a series, or a folder holding one text note
    - and there is no size cap anywhere behind a line that anybody in the
    channel can paste.
    """

    def rar_rows(self):
        with open(self.rar_path(), encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.startswith("!")]

    def test_an_album_folder_is_packable(self):
        self.add("Music/Artist/Album/Track.flac")

        self.assertTrue(self.generate())

        self.assertTrue(any("Album" in row for row in self.rar_rows()))

    def test_a_film_folder_is_not(self):
        """Belt: with the split on, a film is not even in the data the !rar
        rows are built from, so this passes on the split alone. The gate is
        what covers the case below, where the split is off."""
        self.add("Films/Some Film (2021)/Some Film.mkv")

        self.assertTrue(self.generate())

        self.assertEqual([row for row in self.rar_rows() if "Some Film" in row], [])

    def test_a_film_folder_is_not_packable_with_the_split_turned_off(self):
        """Braces. SEPARATE_VIDEO_LIST off puts film back in the same list as
        everything else - which is exactly the configuration an operator who
        keeps films in their own folders will run - and then the only thing
        standing between a stranger and "pack me forty gigabytes" is this
        gate. A mutation deleting it passed every other test in this class."""
        self.set_config(SEPARATE_VIDEO_LIST=False)
        self.add("Films/Some Film (2021)/Some Film.mkv")

        self.assertTrue(self.generate())

        self.assertIn("Some Film.mkv", self.read_list())
        self.assertEqual([row for row in self.rar_rows() if "Some Film" in row], [],
                         "a film folder is packable when the split is off")

    def test_the_shipped_packable_set_holds_no_video(self):
        """Derived from the two settings rather than a list of names here.
        One format in both would make its folder packable, and with the split
        off that is a folder of films."""
        video = {ext.lower()
                 for ext in config.SHIPPED_VALUES["LIST_VIDEO_EXTENSIONS"]}
        packable = {ext.lower()
                    for ext in config.SHIPPED_VALUES["RAR_EXTENSIONS"]}

        self.assertEqual(video & packable, set())

    def test_a_folder_of_notes_is_not(self):
        """The case that made this urgent: everything is listed now, so a
        folder holding one text file used to earn a !rar row."""
        self.add("Docs/Notes/liner notes.txt", b"data")

        self.assertTrue(self.generate())

        self.assertEqual([row for row in self.rar_rows() if "Notes" in row], [])

    def test_a_film_beside_an_album_does_not_make_the_album_unpackable(self):
        """The gate adds a condition; it must not remove one."""
        self.add("Mixed/Concert/Live Set.flac")
        self.add("Mixed/Concert/Live Set.mkv")

        self.assertTrue(self.generate())

        self.assertTrue(any("Concert" in row for row in self.rar_rows()))

    def test_a_film_is_still_requestable_by_name(self):
        """This decides PACKING and nothing else. Refusing to pack a film
        folder must not make the film itself unavailable."""
        self.add("Films/Some Film (2021)/Some Film.mkv")

        self.assertTrue(self.generate())

        self.assertIn("Some Film.mkv", self.read_video_list())

    def test_the_packable_set_is_its_own_setting(self):
        """Not "whatever the scan indexed", which is what it was."""
        self.set_config(RAR_EXTENSIONS=[".flac"])
        self.add("Music/Lossy/Track.mp3")
        self.add("Music/Lossless/Track.flac")

        self.assertTrue(self.generate())

        rows = self.rar_rows()
        self.assertTrue(any("Lossless" in row for row in rows))
        self.assertFalse(any("Lossy" in row for row in rows))

    def test_an_empty_packable_set_makes_nothing_packable(self):
        """A real configuration - "index everything, pack nothing" - and not
        one that should fall back to packing everything.

        THE ALBUM LIST IS NOT WRITTEN AT ALL now, where this used to assert it
        was written with no rows. Both say nothing is packable; the file's
        absence says it better. An album list holding only its own masthead is
        four dead lines and a heading with nothing behind it, and it shipped
        inside the archive every user downloads - see #383, and the identical
        reasoning already applied when RAR_ENABLED is off."""
        self.set_config(RAR_EXTENSIONS=[])
        self.add("Music/Artist/Album/Track.flac")

        self.assertTrue(self.generate())

        published = [n for n in os.listdir(self.tree.lists) if "-RAR-" in n]

        self.assertEqual(published, [],
                         "an album list that can never hold a row was still "
                         "published")

    def test_the_rest_of_the_list_is_unaffected_by_that(self):
        """Guard on the guard: a build that produced nothing at all would also
        satisfy the assertion above."""
        self.set_config(RAR_EXTENSIONS=[])
        self.add("Music/Artist/Album/Track.flac")

        self.assertTrue(self.generate())

        rows = [line for line in self.read_list().splitlines()
                if line.startswith("!")]

        self.assertTrue(rows, "the file list itself went missing too")


class RarExtensionsIsAGateNotADisplayRule(MasterListCase):
    """The claim four places in this branch make, finally enforced.

    RAR_EXTENSIONS decided whether update_list WROTE a "!<nick> !rar <folder>"
    row. It never decided whether one would be HONOURED - dcc.py's whole gate
    was RAR_ENABLED, containment, and "not an artist root", so a folder kept
    deliberately out of the album list was packed happily by anyone who named
    it.

    Harmless while nobody could name one. The film list publishes folder
    headings inside the archive every user downloads, and list_heading_parts()
    strips the prefix, so a heading pastes straight back as a request - which
    is exactly how a folder excluded from the album list became reachable.
    That was an unbounded pack behind a line anybody in the channel could
    send; MAX_RAR_FOLDER_SIZE is the other half of it, and PackingHasACeiling
    below covers that half.

    Found by audit. defaults.py, INSTALL.md, the public changelog and
    test_a_film_folder_is_not_packable_with_the_split_turned_off all asserted
    this was the defence while it was not implemented.
    """

    def test_the_gate_is_asked_on_the_request_path_not_only_the_writer(self):
        """Read out of dcc.py: driving a real pack needs a socket, a peer and
        a rar binary, and the one thing that matters is that the request path
        consults the setting at all. It did not."""
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as fh:
            code = "\n".join(line.split("#", 1)[0]
                              for line in fh.read().splitlines())

        rar_block = code.split('if requested_file.lower().startswith("!rar ")', 1)[1]
        rar_block = rar_block.split("def ", 1)[0]

        self.assertIn("rar_extensions()", rar_block,
                      "the !rar request path never consults RAR_EXTENSIONS, so "
                      "a folder kept out of the album list is still packable "
                      "by anyone who names it")
        self.assertIn("is_packable_file(", rar_block)

    def test_the_refusal_says_the_files_are_still_available(self):
        """Refusing a pack must not read as refusing the content: every file
        in that folder is still listed and still requestable by name."""
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as fh:
            source = fh.read()

        self.assertIn("still be requested by name", source)


class TheBuildSaysWhenNamesCollide(MasterListCase):
    """A request names a FILE, not a path - a bare filename is all the list
    gives a requester to copy - so the resolver serves the first folder it
    finds that name under, and every later copy is listed, looks requestable,
    and can never be sent. The requester does not even get an error; they get
    the other file.

    The dashboard has answered this since #164. This is the half for operators
    who never open it.
    """

    def build_output(self):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            self.assertTrue(self.generate())
        return buffer.getvalue()

    def test_a_collision_is_reported(self):
        self.use_empty_library()
        self.add(os.path.join("Artist", "Album One", "01 - Intro.flac"))
        self.add(os.path.join("Artist", "Album Two", "01 - Intro.flac"))

        output = self.build_output()

        self.assertIn("appear under more than one folder", output)
        self.assertIn("1 filename(s)", output)

    def test_a_clean_library_says_nothing(self):
        """A warning on every build is a warning nobody reads."""
        self.use_empty_library()
        self.add(os.path.join("Artist", "Album One", "01 - Intro.flac"))
        self.add(os.path.join("Artist", "Album Two", "02 - Reprise.flac"))

        self.assertNotIn("more than one folder", self.build_output())

    def test_a_name_in_both_lists_counts(self):
        """The film list is read by the SAME resolver, second - so a name in
        the music list and the film list resolves to the music copy, and the
        film row can never be sent. Counting the two lists separately would
        miss exactly the collisions the split introduced."""
        self.use_empty_library()
        self.add(os.path.join("Artist", "Album", "Bonus.mkv"))
        self.add(os.path.join("Artist", "Album", "Bonus.flac"))
        # Same NAME under two folders, one of each kind.
        self.add(os.path.join("Films", "Feature", "Bonus.mkv"))

        output = self.build_output()

        self.assertIn("appear under more than one folder", output)

    def test_the_count_comes_from_the_same_predicate_the_view_uses(self):
        """Two answers to one question is how they drift. The dashboard
        resolves headings to real paths on top of this; the question of WHICH
        names collide is asked in list.py, not a second time here.

        The name it looks for changed with #463: the build wants the COUNT and
        was building a second full copy of the library as dicts to get it.
        count_duplicate_filenames() answers the same question by the same
        definition - and the test below is what holds the two to that, since
        an identifier appearing in a file is not agreement."""
        with io.open(os.path.join(REPO_ROOT, "update_list.py"), encoding="utf-8") as fh:
            code = chr(10).join(line.split("#", 1)[0]
                                for line in fh.read().splitlines())

        self.assertIn("count_duplicate_filenames(", code)
        self.assertNotIn("folders_by_name", code,
                         "the build is answering the question itself again")


class TwoListsOverTwoFolderSets(MasterListCase):
    """#26 stage 2. The build takes a list name; the driver runs every list."""

    def setUp(self):
        super().setUp()
        self.store = os.path.join(self.tree.root, "lists.json")
        self.set_config(LISTS_FILE=self.store)
        self.use_empty_library()

    def define(self, *entries):
        import json
        with io.open(self.store, "w", encoding="utf-8") as handle:
            json.dump(list(entries), handle)

    def library_at(self, name, *relative):
        root = os.path.join(self.tree.root, name)
        for rel in relative:
            path = os.path.join(root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(b"\0" * 2048)
        return root

    def indexes_in(self, directory):
        return sorted(n for n in os.listdir(directory)
                      if n.startswith(config.LIST_BASE_NAME)
                      and n.endswith(".txt")
                      and "-RAR-" not in n
                      and f"-{list_mod.VIDEO_LIST_MARKER}-" not in n)

    def test_each_list_is_built_from_its_own_folders(self):
        music = self.library_at("music-lib", os.path.join("Artist", "Album", "Track.flac"))
        films = self.library_at("film-lib", os.path.join("Other", "Elsewhere.flac"))
        self.define(
            {"name": "Music", "primary": True,
             "folders": [{"name": "M", "path": music}]},
            {"name": "Films", "folders": [{"name": "F", "path": films}]})

        self.assertTrue(update_list.generate_all_lists(log=lambda *_a: None))

        with io.open(os.path.join(self.tree.lists,
                                  self.indexes_in(self.tree.lists)[0]),
                     encoding="utf-8") as handle:
            primary = handle.read()
        films_dir = os.path.join(self.tree.lists, "Films")
        with io.open(os.path.join(films_dir, self.indexes_in(films_dir)[0]),
                     encoding="utf-8") as handle:
            secondary = handle.read()

        self.assertIn("Track.flac", primary)
        self.assertNotIn("Elsewhere.flac", primary)
        self.assertIn("Elsewhere.flac", secondary)
        self.assertNotIn("Track.flac", secondary)

    def test_the_primary_writes_where_it_always_did(self):
        """Nothing moves for an existing install: no upgrade migrates
        anything, and a single-list operator cannot tell this landed."""
        music = self.library_at("music-lib", os.path.join("Artist", "Album", "Track.flac"))
        self.define({"name": "Music", "primary": True,
                     "folders": [{"name": "M", "path": music}]},
                    {"name": "Films", "folders": []})

        self.assertTrue(update_list.generate_all_lists(log=lambda *_a: None))

        self.assertTrue(self.indexes_in(self.tree.lists),
                        "the primary list did not land in LOCAL_LIST_DIR")

    def test_each_list_keeps_its_own_side_files(self):
        """The size and byte count describe ONE list. Sharing them would have
        each build overwrite the other's advertised size."""
        music = self.library_at("music-lib", os.path.join("A", "B", "One.flac"))
        films = self.library_at("film-lib", os.path.join("F", "Film.mkv"),
                                os.path.join("F", "Second.mkv"))
        self.define({"name": "Music", "primary": True,
                     "folders": [{"name": "M", "path": music}]},
                    {"name": "Films", "folders": [{"name": "F", "path": films}]})

        self.assertTrue(update_list.generate_all_lists(log=lambda *_a: None))

        self.assertTrue(os.path.exists(list_mod.size_file_path()))
        self.assertTrue(os.path.exists(list_mod.size_file_path("Films")))
        self.assertNotEqual(list_mod.size_file_path(),
                            list_mod.size_file_path("Films"))

    def test_a_list_with_no_folders_is_not_a_failure(self):
        """It publishes an empty list, which is exactly what was configured.
        Reporting that as a failed rebuild would send the operator looking for
        a fault they do not have."""
        music = self.library_at("music-lib", os.path.join("A", "B", "One.flac"))
        self.define({"name": "Music", "primary": True,
                     "folders": [{"name": "M", "path": music}]},
                    {"name": "Empty", "folders": []})

        self.assertTrue(update_list.generate_all_lists(log=lambda *_a: None))

    def test_one_list_failing_does_not_stop_the_others(self):
        """A list whose folder is on an unavailable mount must not take down
        the list whose folder is on a local disk. Driven at the driver's own
        seam: the failures worth isolating are the ones the scan cannot
        predict, and a contrived filesystem would test the scan instead."""
        music = self.library_at("music-lib", os.path.join("A", "B", "One.flac"))
        self.define({"name": "Music", "primary": True,
                     "folders": [{"name": "M", "path": music}]},
                    {"name": "Broken", "folders": [{"name": "B", "path": music}]})
        real = update_list.generate_master_list
        built = []

        def one_fails(name=None):
            if name == "Broken":
                return False
            built.append(name)
            return real(name)

        update_list.generate_master_list = one_fails
        self.addCleanup(setattr, update_list, "generate_master_list", real)

        said = []
        ok = update_list.generate_all_lists(log=said.append)

        self.assertFalse(ok, "a failed list must be reported")
        self.assertEqual(built, ["Music"], "the healthy list was not built")
        self.assertIn("Broken", chr(10).join(said))

    def test_a_list_that_raises_is_caught_and_named(self):
        """The backstop. One list's unexpected failure is the difference
        between "two of your three rebuilt" and "the update crashed"."""
        music = self.library_at("music-lib", os.path.join("A", "B", "One.flac"))
        self.define({"name": "Music", "primary": True,
                     "folders": [{"name": "M", "path": music}]},
                    {"name": "Boom", "folders": [{"name": "B", "path": music}]})
        real = update_list.generate_master_list

        def one_raises(name=None):
            if name == "Boom":
                raise RuntimeError("the mount went away")
            return real(name)

        update_list.generate_master_list = one_raises
        self.addCleanup(setattr, update_list, "generate_master_list", real)

        said = []
        ok = update_list.generate_all_lists(log=said.append)

        self.assertFalse(ok)
        self.assertIn("the mount went away", chr(10).join(said))
        self.assertTrue(self.indexes_in(self.tree.lists))

    def test_a_single_list_install_takes_the_same_path_as_before(self):
        """One call with no name - byte for byte what running this script has
        always done. The loop is what a second list turns on."""
        self.library_at("music-lib")
        self.set_config(FILE_DIRECTORY=self.library_at(
            "solo-lib", os.path.join("A", "B", "One.flac")))
        calls = []
        real = update_list.generate_master_list

        def watched(name=None):
            calls.append(name)
            return real(name)

        update_list.generate_master_list = watched
        self.addCleanup(setattr, update_list, "generate_master_list", real)

        self.assertTrue(update_list.generate_all_lists(log=lambda *_a: None))

        self.assertEqual(calls, [None])

    def test_a_prune_never_reaches_another_lists_directory(self):
        """Every list writes the SAME filenames - same base name, same date -
        just in different directories. That makes a cross-list prune almost
        invisible: list B's prune scanning the root would find list A's index
        under a name B's own keep set also holds, and delete nothing.

        So this watches the scope directly rather than the outcome. A stale
        file in each place, one build, and only the built list's own stale
        file may go.
        """
        music = self.library_at("music-lib", os.path.join("A", "B", "One.flac"))
        self.define({"name": "Music", "primary": True,
                     "folders": [{"name": "M", "path": music}]},
                    {"name": "Films", "folders": [{"name": "F", "path": music}]})
        films_dir = os.path.join(self.tree.lists, "Films")
        os.makedirs(films_dir, exist_ok=True)
        stale = f"{config.LIST_BASE_NAME}-2020-01-01.txt"
        for where in (self.tree.lists, films_dir):
            with io.open(os.path.join(where, stale), "w", encoding="utf-8") as handle:
                handle.write("superseded")

        self.assertTrue(update_list.generate_master_list("Films"))

        self.assertFalse(os.path.exists(os.path.join(films_dir, stale)),
                         "the built list did not prune its own superseded index")
        self.assertTrue(os.path.exists(os.path.join(self.tree.lists, stale)),
                        "the prune reached into another list's directory")

class PackingHasACeiling(MasterListCase):
    """MAX_RAR_FOLDER_SIZE. Nothing bounded a pack before it.

    RAR_TIMEOUT was the only thing that ever stopped one, and by the time it
    fires the archive is already on disk in TMP_ZIP_DIR, the single pack slot
    has been held for half an hour, and the requester has had no answer.
    """

    def folder(self, name, count, size):
        import os as os_mod
        path = os_mod.path.join(self.tree.music, name)
        os_mod.makedirs(path, exist_ok=True)
        for i in range(count):
            with open(os_mod.path.join(path, f"track{i}.flac"), "wb") as fh:
                fh.write(b"\0" * size)
        return path

    def test_a_folder_under_the_cap_packs(self):
        path = self.folder("Small", 4, 1024)

        over, measured = update_list.pack_size_over(path, 1024 * 1024)

        self.assertFalse(over)
        self.assertEqual(measured, 4096)

    def test_a_folder_over_it_does_not(self):
        path = self.folder("Big", 8, 1024)

        over, measured = update_list.pack_size_over(path, 2048)

        self.assertTrue(over)
        self.assertGreater(measured, 2048)

    def test_it_counts_what_the_pack_will_actually_take(self):
        """`rar a <dir>` takes the directory AND everything under it, so a
        check that only read the top level would measure something other than
        what gets packed - and the difference is exactly where a huge folder
        hides."""
        import os as os_mod
        path = self.folder("Nested", 1, 1024)
        deep = os_mod.path.join(path, "CD2", "Bonus")
        os_mod.makedirs(deep, exist_ok=True)
        with open(os_mod.path.join(deep, "hidden.flac"), "wb") as fh:
            fh.write(b"\0" * 8192)

        over, measured = update_list.pack_size_over(path, 4096)

        self.assertTrue(over, "a subfolder's contents were not counted")

    def test_it_stops_as_soon_as_the_cap_is_passed(self):
        """The answer wanted is a yes or no, not a total, and the folder this
        is most useful on is the enormous one - so walking all of it to
        produce a number nobody reads is the one cost worth avoiding."""
        path = self.folder("Enormous", 60, 1024)

        over, measured = update_list.pack_size_over(path, 2048)

        self.assertTrue(over)
        self.assertLess(measured, 60 * 1024 // 2,
                        "the whole folder was measured after the answer was known")

    def test_no_cap_configured_costs_nothing(self):
        """An operator who has not set one must not pay a recursive walk on
        every request."""
        import os as os_mod
        walked = []
        real = os_mod.walk
        os_mod.walk = lambda *a, **k: (walked.append(a), real(*a, **k))[1]
        self.addCleanup(setattr, os_mod, "walk", real)

        for cap in (0, None, -1):
            with self.subTest(cap=cap):
                self.assertEqual(update_list.pack_size_over(self.tree.music, cap),
                                 (False, 0))
        self.assertEqual(walked, [], "the disk was walked with no cap set")

    def test_a_file_it_cannot_size_is_skipped_not_raised_on(self):
        """This runs on the REQUEST path, where the alternative to an answer
        is a user who gets no reply at all."""
        import os as os_mod
        path = self.folder("Unreadable", 3, 1024)
        real = os_mod.path.getsize

        def one_fails(p):
            if "track1" in str(p):
                raise OSError("locked by another process")
            return real(p)

        os_mod.path.getsize = one_fails
        self.addCleanup(setattr, os_mod.path, "getsize", real)

        over, measured = update_list.pack_size_over(path, 1024 * 1024)

        self.assertFalse(over)
        self.assertEqual(measured, 2048)

    def test_the_request_path_asks_before_it_queues(self):
        """Read out of dcc.py. Accepting the request and finding out at pack
        time costs a pack slot, half an hour of RAR_TIMEOUT, a part-written
        archive in TMP_ZIP_DIR, and still ends with the requester told nothing
        useful. The size is knowable at request time."""
        with io.open(os.path.join(REPO_ROOT, "dcc.py"), encoding="utf-8") as fh:
            code = "\n".join(line.split("#", 1)[0]
                              for line in fh.read().splitlines())

        rar_block = code.split('if requested_file.lower().startswith("!rar ")', 1)[1]
        rar_block = rar_block.split("def ", 1)[0]

        self.assertIn("pack_size_over(", rar_block,
                      "the !rar request path never consults MAX_RAR_FOLDER_SIZE")
        self.assertLess(rar_block.index("pack_size_over("),
                        rar_block.index("with queue_lock"),
                        "the size is checked after the request is already queued")

    def test_the_shipped_default_passes_a_real_album_and_refuses_a_library(self):
        """A default that refused ordinary albums would break a working
        feature on upgrade; one that passed everything would not be a cap."""
        cap = config.MAX_RAR_FOLDER_SIZE

        self.assertGreater(cap, 5 * 1024 ** 3, "a large box set would be refused")
        self.assertLess(cap, 100 * 1024 ** 3, "not a cap on anything real")


class TheHelperIsTheOnlyPredicate(unittest.TestCase):
    """scripts/setup_check.py counts the library before the first run.

    It had its own copy of the hardcoded pair, so widening the scan without
    widening the count would have reported a healthy library and then built an
    empty list - the same silent shape the setting exists to remove.
    """

    def test_the_preflight_count_asks_update_list(self):
        with io.open(os.path.join(REPO_ROOT, "scripts", "setup_check.py"),
                     encoding="utf-8") as handle:
            code = "\n".join(line.split("#", 1)[0]
                              for line in handle.read().splitlines())

        self.assertIn("update_list.is_listed_file(f)", code)
        self.assertNotIn('(".mp3", ".flac")', code,
                         "setup_check still carries its own copy of the pair")


class WriterAndReaderAgree(MasterListCase):
    """update_list writes the file; list.py counts it for the advert.

    They are in different modules with no shared constant, so the only thing
    keeping them consistent is that every request line starts with "!" and no
    header does. Worth asserting, because the advert's file count is the number
    users see in channel.
    """

    def test_the_advert_count_matches_the_files_on_disk(self):
        for extra in ("A/one.flac", "A/two.mp3", "B/three.flac"):
            self.add(extra)
        self.assertTrue(self.generate())
        count, _date, _size, _raw = list_mod.get_file_count_date_size_and_raw_bytes()
        self.assertEqual(count, 5, "two from the tree plus three added")
        self.assertEqual(len(self.request_lines()), 5)

    def test_the_header_count_matches_the_request_lines(self):
        self.add("A/extra.flac")
        self.assertTrue(self.generate())
        header = self.read_list().split("\n")[0]
        self.assertIn(f"List of {len(self.request_lines()):,} Files", header)

    def test_no_header_line_is_mistaken_for_a_request(self):
        """The reader counts every line starting with "!"; headers must not."""
        self.assertTrue(self.generate())
        for line in self.read_list().split("\n")[:3]:
            with self.subTest(line=line):
                self.assertFalse(line.startswith("!"),
                                 "a header starting with ! would inflate the advert count")

    def test_the_album_list_is_excluded_from_the_count(self):
        """find_latest_list skips -RAR-; if it did not, albums would be counted twice."""
        self.assertTrue(self.generate())
        self.assertNotIn("-RAR-", os.path.basename(list_mod.find_latest_list()))


class TheRequestTriggerIsStable(MasterListCase):
    """An invariant that currently holds by accident, and is worth keeping.

    The list stamps its request lines with config.NICKNAME. irc.py rebinds
    NICKNAME to ALT_NICKNAME after a 433 collision, and ALT_NICKNAME is
    deliberately NOT one of get_bot_aliases() - so a list stamped with it goes
    dead the moment the bot recovers its main nick.

    Production is safe because handle_list_update_request runs update_list.py as
    a SUBPROCESS, which imports a fresh config where NICKNAME is the file default
    and equals ORIGINAL_NICK, which IS an alias. Nothing states that dependency,
    and "why are we spawning a subprocess for this?" is a very natural cleanup.
    """

    def test_production_generates_the_list_in_a_subprocess(self):
        """That the handler RUNS update_list.py as a subprocess, not that
        commands.py contains the text "subprocess.run".

        The previous version sliced the source from "def
        handle_list_update_request" and asserted the substring appeared
        somewhere after it. A comment saying "we use subprocess.run here"
        passed. So did wrapping the real call in `if False:`.

        What the substring stood for is load-bearing, and this class's own
        docstring explains why: in-process generation would stamp the list
        with the LIVE nick, which after a 433 collision is the alt nick, and
        the alt nick is not one of the aliases the bot answers to. The list
        would be built and then be unrequestable.
        """
        import subprocess
        import time as time_mod
        import types

        calls = []
        real_popen = subprocess.Popen

        class FakeChild:
            """Enough of Popen for run_watching_for_a_stall() to finish on its
            first communicate(). Popen, not run: the child is watched rather
            than timed now, so that is where it is actually started - and this
            test is about it being a SEPARATE PROCESS at all, which is the
            part that must not quietly become an in-process call."""

            returncode = 0

            def communicate(self, timeout=None):
                return "", ""

            def kill(self):
                pass

        def recorder(args, **kwargs):
            calls.append(args)
            return FakeChild()

        subprocess.Popen = recorder
        self.addCleanup(setattr, subprocess, "Popen", real_popen)

        commands.handle_list_update_request("operator", "#channel", authorised=True)

        # The handler dispatches into a daemon thread and returns immediately.
        # Polled rather than joined: a thread-set diff also catches whatever
        # unrelated daemon threads the rest of the suite has running, and
        # joining one of those waits out the whole timeout for nothing.
        deadline = time_mod.time() + 10
        while not calls and time_mod.time() < deadline:
            time_mod.sleep(0.01)

        self.assertTrue(calls, "the list was not rebuilt in a subprocess")
        launched = [a for a in calls
                    if any("update_list.py" in str(part) for part in a)]
        self.assertTrue(launched, f"a subprocess ran, but not update_list.py: {calls}")

    def test_the_default_nick_is_an_alias_so_lists_keep_working(self):
        config.NICKNAME = "DCCore_"          # after a 433 collision
        config.ORIGINAL_NICK = "DCCore"
        self.assertIn("dccore", irc.get_bot_aliases(),
                      "a list stamped with the config default must stay requestable")

    def test_the_alt_nick_is_not_an_alias(self):
        """Which is why the subprocess matters: it never stamps the alt nick."""
        config.NICKNAME = "DCCore"
        config.ORIGINAL_NICK = "DCCore"
        config.ALT_NICKNAME = "DCCore_"
        self.assertNotIn("dccore_", irc.get_bot_aliases())

    def test_in_process_generation_stamps_whatever_nick_is_live(self):
        """Documents the hazard rather than asserting it is fine.

        If this ever becomes the production path, every line of the published
        list is stamped with a trigger the bot stops answering to on recovery.
        """
        config.NICKNAME = "DCCore_"
        self.assertTrue(self.generate())
        self.assertTrue(all(line.startswith("!DCCore_ ") for line in self.request_lines()),
                        "in-process generation follows the live nick - hence the subprocess")


class TheAlbumList(MasterListCase):
    def test_one_line_per_album_folder(self):
        self.add("Metallica/Ride The Lightning (1984)/01.flac")
        self.add("Metallica/Ride The Lightning (1984)/02.flac")
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            rar_lines = [l for l in handle.read().split("\n") if l.startswith("!")]
        self.assertEqual(len(rar_lines), len(set(rar_lines)),
                         "an album must not be advertised twice")

    def test_a_multidisc_album_is_advertised_once(self):
        """What written_rar_folders is actually for.

        Two files in ONE folder are already deduplicated by the folder-change
        check. The set only earns its keep when two DIFFERENT folders clean down
        to the same album root - which is exactly what CD1/CD2 do.
        """
        self.add("Metallica/Black Album/CD1/01.flac")
        self.add("Metallica/Black Album/CD2/01.flac")
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            rar_lines = [l for l in handle.read().split("\n") if l.startswith("!")]
        self.assertEqual(len(rar_lines), len(set(rar_lines)),
                         "a multidisc album must not appear twice in the !rar list")

    def test_it_names_the_rar_trigger(self):
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            self.assertIn("!rar", handle.read())

    def test_multidisc_truncation_still_works(self):
        """Defect guard for the segment-anchored rewrite: the ordinary
        "CD1"/"CD2" case must keep working exactly as before."""
        self.add("Metallica/Black Album/CD1/01.flac")
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            rar_text = handle.read()
        self.assertIn("Black Album", rar_text)
        self.assertNotIn("CD1", rar_text,
                         "the disc subfolder must still be stripped from the row")

    def test_discography_is_not_mistaken_for_disc(self):
        """#162 finding #16, the headline repro: a substring match let
        "\\disc" fire on "\\Discography", collapsing the row to the ARTIST
        root - which dcc.py refuses outright - and leaving the real album
        with no requestable row at all (written_rar_folders deduplicates
        on the wrong, truncated string)."""
        self.add("Pink Floyd/Discography 1967-2014/The Wall/01.flac")
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            rar_text = handle.read()
        self.assertIn("The Wall", rar_text,
                     "the specific album must still be offered, not collapsed "
                     "to the artist root")
        # The old bug's own row - the refused artist root alone - must not
        # appear as a substitute for the real one.
        rar_lines = [l for l in rar_text.split("\n") if l.startswith("!")]
        self.assertNotIn("!DCCore !rar D:\\MEDIA\\Pink Floyd\\", rar_lines)

    def test_a_media_markt_style_folder_is_not_mistaken_for_media(self):
        """The same substring bug, the "\\media" box word this time."""
        self.add("Various Artists/Media Markt Hits/01.flac")
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            rar_text = handle.read()
        self.assertIn("Media Markt Hits", rar_text)

    def test_a_genre_first_library_still_offers_the_specific_album(self):
        """The audit's own 'aggravating variant': on a genre-first layout
        the truncated row had exactly two segments and was ACCEPTED by
        dcc.py, packing the whole discography instead of the one album -
        worse than the plain artist-root case, because nothing about it
        looked refused."""
        self.add("Rock/Pink Floyd/Discography/The Wall/01.flac")
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            rar_text = handle.read()
        self.assertIn("The Wall", rar_text)
        rar_lines = [l for l in rar_text.split("\n") if l.startswith("!")]
        self.assertNotIn("!DCCore !rar D:\\MEDIA\\Rock\\Pink Floyd\\", rar_lines,
                         "must not collapse to the whole-discography folder")

    def test_truncation_never_collapses_to_the_artist_root(self):
        """A folder shaped like "Artist/CD1" (no album subfolder at all)
        would collapse to the single-segment artist root if truncated -
        exactly what dcc.py refuses. Must be left untruncated instead."""
        self.add("SomeArtist/CD1/01.flac")
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            rar_text = handle.read()
        rar_lines = [l for l in rar_text.split("\n") if l.startswith("!")]
        self.assertNotIn("!DCCore !rar D:\\MEDIA\\SomeArtist\\", rar_lines,
                         "must never offer the bare artist root")

    def test_earliest_matching_segment_wins_not_list_order(self):
        """Defect: the old code tried box words in a fixed LIST order
        ('cd' before 'disc') and truncated at whichever one it found
        first IN THAT LIST, anywhere in the string - not whichever one
        appears earliest in the actual path. A folder with "Disc 1" before
        a later "CD 2" used to truncate at the LATER "CD 2" (because "cd"
        is checked first), keeping the wrong, more specific-looking folder
        instead of the actual, earlier album boundary."""
        self.add("Artist/Album/Disc 1/CD 2/01.flac")
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            rar_text = handle.read()
        rar_lines = [l for l in rar_text.split("\n") if l.startswith("!")]

        # Every path now leads with the folder's label (#164). Derived rather
        # than written out, so this test says "the album under its folder" and
        # not "the folder happens to be called music".
        label = library.folders()[0].name

        # "Disc 1" is the earliest matching segment - truncating there leaves
        # the label plus Artist/Album, the real album.
        self.assertIn(f"!DCCore !rar D:\\MEDIA\\{label}\\Artist\\Album\\", rar_lines)
        self.assertNotIn(f"!DCCore !rar D:\\MEDIA\\{label}\\Artist\\Album\\Disc 1\\",
                         rar_lines,
                         "must not stop at the later 'CD' match instead of "
                         "the earlier 'Disc' one")

    def test_a_disc_directly_under_an_artist_does_not_collapse_to_the_root(self):
        """The threshold that moved with the label, and why it had to.

        It means "leave at least two segments below the folder" - artist and
        album. rel_dir now begins with the label, so every index shifted by
        one: left at its old value, this shape would truncate to
        <label>/Artist, which is the artist root dcc.py refuses outright.
        The album would then have no requestable row at all - the exact
        failure the threshold exists to prevent.
        """
        self.add("SoloArtist/Disc 1/01.flac")
        self.assertTrue(self.generate())
        with open(self.rar_path(), encoding="utf-8") as handle:
            rar_lines = [l for l in handle.read().split("\n") if l.startswith("!")]

        label = library.folders()[0].name

        self.assertNotIn(f"!DCCore !rar D:\\MEDIA\\{label}\\SoloArtist\\", rar_lines,
                         "truncated to the artist root, which cannot be requested")
        self.assertIn(f"!DCCore !rar D:\\MEDIA\\{label}\\SoloArtist\\Disc 1\\",
                      rar_lines,
                      "the untruncated path is still a request dcc.py serves")


class PruningSupersededLists(MasterListCase):
    """_prune_superseded_lists deletes files, so what it spares matters."""

    def touch(self, name, body="x"):
        path = os.path.join(self.tree.lists, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
        return path

    def test_yesterdays_lists_are_removed(self):
        old = self.touch(f"{config.LIST_BASE_NAME}-2020-01-01.txt")
        old_zip = self.touch(f"{config.LIST_BASE_NAME}-2020-01-01.zip")
        self.assertTrue(self.generate())
        self.assertFalse(os.path.exists(old))
        self.assertFalse(os.path.exists(old_zip))

    def test_todays_new_lists_are_kept(self):
        self.assertTrue(self.generate())
        self.assertTrue(os.path.exists(self.list_path()))
        self.assertTrue(os.path.exists(self.rar_path()))

    def test_unrelated_files_are_never_touched(self):
        """The list directory is a real directory someone may keep things in."""
        keepers = [self.touch("notes.txt"), self.touch("OtherBot-2020-01-01.txt"),
                   self.touch("readme.md"), self.touch("archive.tar.gz")]
        self.assertTrue(self.generate())
        for path in keepers:
            with self.subTest(path=os.path.basename(path)):
                self.assertTrue(os.path.exists(path),
                                "pruning must only remove this bot's own old lists")

    def test_a_file_sharing_the_bots_name_but_not_a_list_is_spared(self):
        """The case the extension check exists for.

        The "unrelated files" test above is spared by the startswith check alone,
        so it never exercised this. What needs the extension test is a file that
        DOES begin with LIST_BASE_NAME and is not a generated list - a log, a
        backup, notes the operator keeps beside the lists.
        """
        keepers = [self.touch(f"{config.LIST_BASE_NAME}.log"),
                   self.touch(f"{config.LIST_BASE_NAME}-notes.md"),
                   self.touch(f"{config.LIST_BASE_NAME}-backup.tar.gz"),
                   self.touch(f"{config.LIST_BASE_NAME}-2020-01-01.txt.bak")]
        self.assertTrue(self.generate())
        for path in keepers:
            with self.subTest(path=os.path.basename(path)):
                self.assertTrue(os.path.exists(path),
                                "pruning must only remove .txt and .zip lists")

    def test_the_side_files_are_not_pruned(self):
        """They do not end in .txt by accident of naming - check anyway."""
        self.assertTrue(self.generate())
        self.assertTrue(os.path.exists(list_mod.size_file_path()))
        self.assertTrue(os.path.exists(list_mod.rawbytes_file_path()))


class TheSwapLeavesNothingBehind(MasterListCase):
    def test_no_temp_files_survive_a_successful_run(self):
        self.assertTrue(self.generate())
        leftovers = [n for n in os.listdir(self.tree.lists) if n.endswith(".new")]
        self.assertEqual(leftovers, [])

    def test_no_temp_files_survive_a_refused_run(self):
        """A scan finding nothing next to a good index is refused; clean up after."""
        self.assertTrue(self.generate())
        empty = os.path.join(self.tree.root, "vanished")
        os.makedirs(empty, exist_ok=True)
        config.FILE_DIRECTORY = empty
        self.assertFalse(self.generate())
        leftovers = [n for n in os.listdir(self.tree.lists) if n.endswith(".new")]
        self.assertEqual(leftovers, [], "a refused run must not litter the list directory")

    def test_the_zip_holds_the_final_names_not_the_temp_ones(self):
        """Users open this archive; ".txt.new" inside it would be visible."""
        self.assertTrue(self.generate())
        zip_name = [n for n in os.listdir(self.tree.lists) if n.endswith(".zip")][0]
        with zipfile.ZipFile(os.path.join(self.tree.lists, zip_name)) as archive:
            names = archive.namelist()
        self.assertTrue(names)
        for name in names:
            with self.subTest(name=name):
                self.assertFalse(name.endswith(".new"))


class UnreadableSubtreeKeepsThePreviousIndex(MasterListCase):
    """#162 finding #15: os.walk()'s default onerror=None silently skips a
    subtree it cannot read (a stale NFS handle, EIO, a revoked ACL) - the
    scan's own file count comes out non-zero regardless, so the "0 files
    found" guard never catches it, and a TRUNCATED index gets published
    over a good one with no error anywhere.

    A real unreadable directory needs real broken permissions, which chmod
    cannot portably simulate in CI (Windows, and this suite runs as root in
    some environments) - so os.walk() is stubbed to call its own onerror
    callback, exactly as a genuinely unreadable subtree would, without
    touching real filesystem permissions at all."""

    def walk_erroring(self, err):
        """Report an unreadable subtree without making one.

        Stubs update_list.walk_with_sizes(), which is the seam the scan
        actually uses. It stubbed os.walk() before the scan stopped
        asking for every size twice - and patching a stdlib function
        globally reached further than it needed to, since shutil.rmtree
        and the harness's own cleanup walk through the same name.
        """
        real_walk = update_list.walk_with_sizes

        def fake_walk(top, onerror=None):
            if onerror is not None:
                onerror(err)
            yield from real_walk(top, onerror=onerror)

        update_list.walk_with_sizes = fake_walk
        self.addCleanup(
            lambda: setattr(update_list, "walk_with_sizes", real_walk))

    def test_a_walk_error_refuses_to_publish(self):
        self.add("Metallica/Black Album/01.flac")
        self.assertTrue(self.generate())
        old_list = self.read_list()

        # Would change the published list's contents if the run went
        # through - proves the refusal, not just an unlucky no-op.
        self.add("Metallica/New Album/02.flac")
        self.walk_erroring(OSError(5, "Input/output error", "/some/unreadable/subtree"))

        result = self.generate()

        self.assertFalse(result)
        self.assertEqual(self.read_list(), old_list,
                         "a partial scan must never overwrite a good index")

    def test_no_walk_error_still_publishes_normally(self):
        """Control: the onerror wiring itself must not refuse a clean scan."""
        self.add("Metallica/Black Album/01.flac")
        self.assertTrue(self.generate())


class AnUnreadableFileIsExcludedNotPublishedAsZeroBytes(MasterListCase):
    """#228: a bare `except: pass` around os.path.getsize() left file_bytes
    at 0 and still appended the entry - permission denied, a dangling
    symlink, or a file removed mid-scan all read as a legitimate 0-byte
    track. The list is what the bot HANDS OUT: publishing it offered a
    download that can only ever fail once someone actually requests it, with
    the library's reported total size quietly short and no log line saying
    why.

    Deliberately the opposite fix from UnreadableSubtreeKeepsThePreviousIndex
    above: THAT is a whole subtree going unreadable, systemic enough to
    refuse the whole run and keep the previous good index. ONE file failing
    getsize() is not - excluding just that file and publishing everything
    else is what the control below checks was not lost along with the bug.
    """

    def getsize_raising_for(self, target_path):
        """Make one file's size unreadable, at the point the scan reads it.

        THAT POINT MOVED. This used to stub os.path.getsize(), which is what
        the scan called - one extra syscall per file for a number the
        directory enumeration had already returned. walk_with_sizes() takes it
        from the DirEntry now, so a getsize stub sits on a function the scan
        no longer reaches, and both tests below would exercise the READABLE
        path while still passing.

        Which is the second time that has happened here. The previous version
        carried a note about silently testing the readable path on Windows,
        because it compared paths without long_path(). A stub has to match the
        way production calls the thing, not the way the test happens to hold
        it - so this one wraps os.scandir and gives the target entry a stat()
        that raises, which is what an unreadable file actually does.
        """
        real_scandir = os.scandir

        def same_file(path):
            """Compared through long_path() on BOTH sides, because that is how
            the code under test walks: scan_root is
            platform_compat.long_path(scan_folder.path), so on Windows every
            entry.path carries the "\\?\\" prefix and a plain normpath
            comparison never matches.

            The version of this helper that stubbed os.path.getsize carried
            the same note, for the same reason, after the same failure. It was
            load-bearing then and it is load-bearing now: without it the stub
            never fires, both tests below exercise the READABLE path, and they
            pass while proving nothing."""
            return (platform_compat.long_path(os.path.normcase(os.path.normpath(path)))
                    == platform_compat.long_path(os.path.normcase(os.path.normpath(target_path))))

        class Unreadable:
            """A DirEntry that answers everything except its size."""

            def __init__(self, entry):
                self._entry = entry
                self.name = entry.name
                self.path = entry.path

            def is_dir(self, follow_symlinks=True):
                return self._entry.is_dir(follow_symlinks=follow_symlinks)

            def is_file(self, follow_symlinks=True):
                return self._entry.is_file(follow_symlinks=follow_symlinks)

            def stat(self, follow_symlinks=True):
                raise OSError(13, "Permission denied", self.path)

        class FakeScandir:
            """A stand-in for os.scandir, and it has to be a real ITERATOR.

            walk_with_sizes() uses it as a context manager, so __enter__ and
            __exit__ are needed - but this replaces os.scandir globally, and
            shutil.rmtree calls next() on the result directly during cleanup.
            A class with only __iter__ satisfies a for-loop and fails there
            with "not an iterator", taking the temporary tree with it.
            """

            def __init__(self, path="."):
                with real_scandir(path) as scanning:
                    self._entries = [
                        Unreadable(entry) if same_file(entry.path) else entry
                        for entry in scanning
                    ]
                self._next = iter(self._entries)

            def __iter__(self):
                return self

            def __next__(self):
                return next(self._next)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        os.scandir = FakeScandir
        self.addCleanup(lambda: setattr(os, "scandir", real_scandir))

    def test_the_unreadable_file_is_left_out(self):
        good_path = self.add("Metallica/Black Album/01.flac")
        bad_path = self.add("Metallica/Black Album/02.flac")
        self.getsize_raising_for(bad_path)

        self.assertTrue(self.generate(),
                        "one bad file must not refuse the whole scan")

        lines = self.request_lines()
        self.assertTrue(any("01.flac" in line for line in lines),
                        "the readable file must still be published")
        self.assertFalse(any("02.flac" in line for line in lines),
                         "the unreadable file must not be published as a "
                         "0-byte entry nobody can ever actually download")

    def test_the_readable_files_total_size_is_not_shorted(self):
        """A second control: the excluded file's phantom 0 bytes must not
        even implicitly participate - the header's total is exactly what the
        readable files (the tree's two defaults plus the one added here) add
        up to, not counting the excluded file at all."""
        good_path = self.add("Metallica/New Album/01.flac", data=b"\x00" * 4096)
        bad_path = self.add("Metallica/New Album/02.flac", data=b"\x00" * 4096)
        self.getsize_raising_for(bad_path)

        self.generate()

        header = self.read_list().split("\n", 1)[0]
        expected_files = len(self.tree.tracks) + 1  # the tree's two defaults, plus the good one
        self.assertIn(f"List of {expected_files} Files", header)


class SideFilesOnlyPublishAfterTheSwap(MasterListCase):
    """#162 finding #32: the size/rawbytes side files used to be written
    BEFORE the os.replace() calls that actually publish the new list, so a
    failed swap rolled the list back to the previous index while the side
    files had already been overwritten with the new (unpublished) scan's
    numbers - an old index wearing a new scan's size."""

    def _side_file_contents(self):
        size_path = os.path.join(self.tree.lists, config.LIST_SIZE_FILE)
        rawbytes_path = os.path.join(self.tree.lists, config.LIST_RAWBYTES_FILE)
        with open(size_path, encoding="utf-8") as handle:
            size = handle.read()
        with open(rawbytes_path, encoding="utf-8") as handle:
            rawbytes = handle.read()
        return size, rawbytes

    def test_a_failed_swap_never_touches_the_side_files(self):
        self.add("Metallica/Black Album/01.flac")
        self.assertTrue(self.generate())
        old_side_files = self._side_file_contents()
        old_list = self.read_list()

        # A bigger scan, so the side files WOULD visibly change if this
        # reached the write - proves the ordering, not an unlucky no-op.
        self.add("Metallica/New Album/02.flac", data=b"\x00" * 999999)

        real_replace = os.replace

        def failing_replace(src, dst):
            raise OSError("simulated replace failure")

        os.replace = failing_replace
        self.addCleanup(lambda: setattr(os, "replace", real_replace))

        result = self.generate()

        self.assertFalse(result)
        self.assertEqual(self.read_list(), old_list,
                         "the previous index must survive a failed swap")
        self.assertEqual(self._side_file_contents(), old_side_files,
                         "the side files must never run ahead of a swap "
                         "that never actually happened")


class DiscardingTempLists(MasterListCase):
    """_discard_temp_lists is the cleanup every refusal path calls.

    One of its three call sites - the "no list was written" guard - is defensive
    and not reachable from a normal run, so the helper is tested directly rather
    than left to whichever branch happens to be exercised.
    """

    def test_it_removes_what_is_there(self):
        paths = []
        for name in ("a.txt.new", "b.txt.new"):
            path = os.path.join(self.tree.lists, name)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("half written")
            paths.append(path)
        update_list._discard_temp_lists(*paths)
        for path in paths:
            with self.subTest(path=os.path.basename(path)):
                self.assertFalse(os.path.exists(path))

    def test_a_missing_file_is_not_an_error(self):
        """Called on paths that may never have been created."""
        update_list._discard_temp_lists(
            os.path.join(self.tree.lists, "never-existed.new"))

    def test_none_and_empty_paths_are_tolerated(self):
        update_list._discard_temp_lists(None, "", None)

    def test_a_file_it_cannot_remove_does_not_raise(self):
        """Cleanup runs on the failure path; it must not raise a second failure."""
        real_remove = os.remove

        def refuse(path):
            raise OSError(13, "Permission denied")

        path = os.path.join(self.tree.lists, "locked.txt.new")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("x")
        os.remove = refuse
        try:
            update_list._discard_temp_lists(path)
        finally:
            os.remove = real_remove
        self.assertTrue(os.path.exists(path), "the stub refused, as intended")


class AwkwardFilenames(MasterListCase):
    """A real library is not made of ASCII."""

    def test_an_apostrophe_survives(self):
        self.add("Artist/Album/A Winter's Tale.flac")
        self.assertTrue(self.generate())
        self.assertIn("A Winter's Tale.flac", self.read_list())

    def test_non_ascii_survives(self):
        self.add("Björk/Homogenic/Jóga.flac")
        self.assertTrue(self.generate())
        self.assertIn("Jóga.flac", self.read_list())

    def test_a_filename_with_spaces_stays_on_one_line(self):
        self.add("Artist/Album/A Very Long Track Name Indeed.flac")
        self.assertTrue(self.generate())
        matches = [l for l in self.request_lines()
                   if "A Very Long Track Name Indeed.flac" in l]
        self.assertEqual(len(matches), 1)

    def test_a_filename_cannot_split_a_request_line(self):
        """Found by CI on Linux; Windows refuses such a name at creation.

        POSIX allows newlines in filenames - only "/" and NUL are forbidden - so
        this is a legal track on the box the daemon runs on. Written verbatim it
        turned one entry into two lines, leaving a truncated entry and an orphan
        fragment, and the list was malformed from there down.
        """
        try:
            self.add("Artist/Album/evil\nname.flac")
        except (OSError, ValueError):
            self.skipTest("this filesystem refuses newlines in filenames")
        self.assertTrue(self.generate())
        lines = self.request_lines()
        self.assertEqual(len(lines), 3, "two real tracks plus the awkward one")
        for line in lines:
            with self.subTest(line=line):
                self.assertEqual(line.count("::INFO::"), 1,
                                 "every request line carries exactly one size marker")


class FlatteningIsWiredIntoTheWriter(MasterListCase):
    """That _one_line exists is not the same as the writer using it.

    The filesystem test can only run where a newline is a legal filename, so on
    Windows it skips and the wiring goes unverified - a mutation removing the
    call from the request line survives there. Stubbing the walk instead of the
    filesystem tests the same path on every platform.
    """

    def walk_yielding(self, *names):
        """Make the scan see `names` in the album folder, whatever is on disk.

        The names this class yields (embedded newlines, surrogateescape
        bytes) generally do not exist as real files - only os.walk() is
        stubbed to report them, deliberately, so the test works on every
        platform including one where such a name cannot actually be created.
        os.path.getsize() is therefore also stubbed here: since #228,
        generate_master_list() excludes a file it cannot read the size of,
        and unwrapped every synthetic name here would raise
        FileNotFoundError and be silently excluded - passing every assertion
        below by leaving nothing to make them fail, not by the flattening
        actually working.
        """
        real_walk = update_list.walk_with_sizes
        album = os.path.abspath(self.tree.album)

        def fake_walk(top, onerror=None):
            for root, files in real_walk(top, onerror=onerror):
                # The scan walks a platform_compat.long_path()-wrapped
                # root (#162 finding #21), which on Windows prefixes every
                # yielded `root` - os.path.abspath() does not strip that,
                # so comparing against the unwrapped `album` below would
                # never match without stripping it here too.
                root_compare = os.path.abspath(root)
                if root_compare.startswith('\\\\?\\'):
                    root_compare = root_compare[4:]
                if root_compare == album:
                    # NAME AND SIZE TOGETHER, which is why the getsize
                    # shim this used to need is gone. These names (a
                    # newline, a lone CR, invalid UTF-8) generally cannot
                    # exist as real files, so the old stub had to make
                    # os.path.getsize answer 2048 for a path that was
                    # never on disk - or every crafted entry raised
                    # FileNotFoundError, was silently excluded, and every
                    # assertion passed by having nothing left to fail on.
                    yield root, [(name, 2048) for name in names]
                else:
                    yield root, files

        update_list.walk_with_sizes = fake_walk
        self.addCleanup(
            lambda: setattr(update_list, "walk_with_sizes", real_walk))

    def test_a_newline_in_a_scanned_name_cannot_split_the_entry(self):
        self.walk_yielding("evil\nname.flac")
        self.assertTrue(self.generate())
        lines = self.request_lines()
        self.assertEqual(len(lines), 1, "one file scanned, one request line")
        self.assertEqual(lines[0].count("::INFO::"), 1)
        self.assertIn("evil name.flac", lines[0])

    def test_a_carriage_return_cannot_split_the_entry(self):
        self.walk_yielding("cr\rname.flac")
        self.assertTrue(self.generate())
        self.assertEqual(len(self.request_lines()), 1)

    def test_every_line_of_the_published_list_is_intact(self):
        """The property that matters: no orphan fragments anywhere in the file."""
        self.walk_yielding("a\nb.flac", "c\rd.mp3", "normal.flac")
        self.assertTrue(self.generate())
        for line in self.read_list().split("\n"):
            stripped = line.strip()
            if not stripped or stripped.startswith("="):
                continue
            if stripped.startswith("!"):
                with self.subTest(line=stripped):
                    self.assertEqual(stripped.count("::INFO::"), 1)
            else:
                with self.subTest(line=stripped):
                    self.assertFalse("::INFO::" in stripped,
                                     "a size marker outside a request line is an orphan")

    def test_a_non_utf8_filename_does_not_abort_the_whole_rebuild(self):
        """#162 finding #4, the filesystem-independent half: the exact
        surrogateescape shape os.walk() hands back for a non-UTF-8 name on
        a real library, exercised through the real writer - not just
        _one_line() in isolation."""
        self.walk_yielding("Bj\udcf6rk.flac", "normal.flac")
        self.assertTrue(self.generate())
        lines = self.request_lines()
        self.assertEqual(len(lines), 2, "both files must survive the scan")
        self.assertIn("normal.flac", self.read_list())


class FlatteningNamesForOneLinePerEntry(unittest.TestCase):
    """_one_line, tested directly so BOTH platforms verify it.

    The filesystem test above can only run where such a filename can be created,
    which is not Windows - so on the Windows jobs it skips and the fix would go
    unverified. These do not touch the filesystem.
    """

    def test_a_newline_becomes_a_space(self):
        self.assertEqual(update_list._one_line("evil\nname.flac"), "evil name.flac")

    def test_a_carriage_return_becomes_a_space(self):
        self.assertEqual(update_list._one_line("cr\rname.flac"), "cr name.flac")

    def test_a_tab_becomes_a_space(self):
        """The list is column-aligned by spaces; a tab would break the layout."""
        self.assertEqual(update_list._one_line("tab\there.flac"), "tab here.flac")

    def test_other_control_characters_go_too(self):
        for code in (0x00, 0x07, 0x1b, 0x7f):
            with self.subTest(code=hex(code)):
                flattened = update_list._one_line(f"a{chr(code)}b.flac")
                self.assertEqual(flattened, "a b.flac")

    def test_ordinary_names_are_returned_unchanged(self):
        for name in ("Enter Sandman.flac", "A Winter's Tale.flac", "track (1984).mp3"):
            with self.subTest(name=name):
                self.assertEqual(update_list._one_line(name), name)

    def test_non_ascii_is_preserved(self):
        """Flattening must not turn a music library into mojibake."""
        for name in ("Jóga.flac", "Björk.mp3", "Sigur Rós - Svefn-g-englar.flac"):
            with self.subTest(name=name):
                self.assertEqual(update_list._one_line(name), name)

    def test_the_result_never_contains_a_line_break(self):
        """The property the whole helper exists for."""
        for name in ("a\nb", "a\r\nb", "\n\n\n", "a\rb\nc"):
            with self.subTest(name=name):
                flattened = update_list._one_line(name)
                self.assertNotIn("\n", flattened)
                self.assertNotIn("\r", flattened)

    def test_a_non_utf8_byte_does_not_raise(self):
        """#162 finding #4. os.walk() on POSIX decodes a filename with
        non-UTF-8 bytes (a CP1252 rip, a bad extraction, a FAT copy) using
        the "surrogateescape" error handler - a lone surrogate codepoint,
        not itself a control character, but not valid UTF-8 either. The
        exact reported repro: '\\udcf6' embedded in an otherwise normal
        name. Before this fix, writing it with a strict UTF-8 encoder
        raised UnicodeEncodeError and abandoned the entire list rebuild."""
        flattened = update_list._one_line("Bj\udcf6rk.flac")
        # Must not raise, and the result must itself be safely UTF-8
        # encodable - the whole point, since this text is about to be
        # written with io.open(..., encoding="utf-8").
        flattened.encode("utf-8")

    def test_a_non_utf8_byte_becomes_a_visible_placeholder_not_silence(self):
        flattened = update_list._one_line("Bj\udcf6rk.flac")
        self.assertNotIn("\udcf6", flattened)
        self.assertIn("Bj", flattened)
        self.assertIn("rk.flac", flattened)

    def test_valid_unicode_is_unaffected_by_the_utf8_sanitisation(self):
        """The sanitisation step must not itself mangle a perfectly good
        name - it is a round-trip through UTF-8, and every one of these
        already IS valid UTF-8."""
        for name in ("Jóga.flac", "Björk.mp3", "Sigur Rós - Svefn-g-englar.flac"):
            with self.subTest(name=name):
                self.assertEqual(update_list._one_line(name), name)


if __name__ == "__main__":
    unittest.main()


class TheFolderRuleMatchesTheFolder(MasterListCase):
    """The ==== rule above and below each folder header used to be a fixed 53
    characters, while the header it framed was never that width.

    Measured against the operator's real 1.21TB library: 4,107 folder headers,
    running 54 to 136 characters and averaging 80. Every single one was wider
    than the rule, so the framing was always ragged.
    """

    def _blocks(self):
        """Every (rule, header, rule) triple in the generated text list."""
        self.assertTrue(update_list.generate_master_list())
        path = list_mod.find_latest_list()
        self.assertIsNotNone(path, "no list was generated")
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            lines = [l.rstrip("\n") for l in handle]

        found = []
        for i in range(1, len(lines) - 1):
            above, header, below = lines[i - 1], lines[i], lines[i + 1]
            if (above and below and set(above) == {"="} and set(below) == {"="}
                    and set(header) != {"="}):
                found.append((above, header, below))
        return found

    def test_every_rule_is_exactly_as_wide_as_its_folder_line(self):
        blocks = self._blocks()
        self.assertTrue(blocks, "the fixture produced no folder headers")
        for above, header, below in blocks:
            self.assertEqual(len(above), len(header),
                             f"rule above is {len(above)}, header is {len(header)}")
            self.assertEqual(len(below), len(header),
                             f"rule below is {len(below)}, header is {len(header)}")

    def test_the_rules_are_not_the_old_fixed_width(self):
        """Guards the specific regression: a 53-character rule against a
        header that is not 53 characters."""
        for above, header, _below in self._blocks():
            if len(header) != 53:
                self.assertNotEqual(
                    len(above), 53,
                    "the rule is back to the old fixed 53 characters")

    def test_a_long_folder_name_still_gets_a_matching_rule(self):
        """The real library's widest header is 136 characters."""
        deep = os.path.join(self.tree.music,
                            "An Artist With A Considerably Longer Name Than Usual",
                            "1997 - An Album Title That Also Runs Long [REMASTER] [16-44]")
        os.makedirs(deep, exist_ok=True)
        with open(os.path.join(deep, "01 - Track.flac"), "wb") as handle:
            handle.write(b"\x00" * 2048)

        widest = max(self._blocks(), key=lambda b: len(b[1]))
        self.assertGreater(len(widest[1]), 53,
                           "fixture did not produce a header wider than the old rule")
        self.assertEqual(len(widest[0]), len(widest[1]))
        self.assertEqual(len(widest[2]), len(widest[1]))
