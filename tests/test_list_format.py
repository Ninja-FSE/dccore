"""config.LIST_FORMAT - txt, zip or rar, for the list people download.

OmenServe has offered the same three for years (the teardown on #69), and
which of them a given person's client opens without complaint still differs.
DCCore only ever built the .zip.

All three are always available. This is NOT tied to RAR_ENABLED: that switch
governs whether the bot will pack an album folder for a stranger on demand -
minutes of CPU and a large temporary file, per request - and packing two text
files once per rebuild is a different job.

The three things that can go wrong are not the packing:

  * the delivered .txt being mistaken for the master index, which would let
    @find offer album rows as though they were tracks and the advert count
    the albums as files;
  * a format that cannot be built taking the bot's list off the air, when a
    list in the other format was right there;
  * "ZIP" or "tar" reaching config, where nothing matches it and the bot
    quietly stops serving a list at all.
"""

import io
import os
import subprocess
import sys
import unittest
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import list as list_mod  # noqa: E402
import platform_compat  # noqa: E402
import settings_file  # noqa: E402
import update_list  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class ListFormatCase(DCCoreTestCase):

    def setUp(self):
        super().setUp()
        self.tree = self.make_tree()
        self.set_config(FILE_DIRECTORY=self.tree.music,
                        LOCAL_LIST_DIR=self.tree.lists,
                        LIST_BASE_NAME="DCCoreTest",
                        NICKNAME="DCCoreTest",
                        RAR_ENABLED=True,
                        LIST_FORMAT="zip")

    def build(self, fmt=None, **overrides):
        """Run a list build and return everything it printed."""
        if fmt is not None:
            overrides["LIST_FORMAT"] = fmt
        if overrides:
            self.set_config(**overrides)
        buffer = io.StringIO()
        from contextlib import redirect_stdout
        with redirect_stdout(buffer):
            built = update_list.generate_master_list()
        self.assertTrue(built, "the list build failed:\n" + buffer.getvalue())
        return buffer.getvalue()

    def lists_dir(self):
        return sorted(f for f in os.listdir(self.tree.lists) if not f.startswith("."))

    def served(self):
        """The basename of what @<nick> would DCC-send right now."""
        path = list_mod.find_latest_list_file()
        return os.path.basename(path) if path else None

    def artifacts(self, suffix):
        return [f for f in self.lists_dir() if f.endswith(suffix)]


class EachFormatBuildsAndIsWhatGetsSent(ListFormatCase):

    def test_zip_is_the_default_and_is_unchanged(self):
        """The behaviour that already existed stays the behaviour nobody has
        to opt into."""
        self.build()

        self.assertEqual(config.LIST_FORMAT, "zip")
        self.assertTrue(self.served().endswith(".zip"), self.lists_dir())

    def test_the_zip_still_carries_both_lists_under_their_published_names(self):
        self.build("zip")

        with zipfile.ZipFile(os.path.join(self.tree.lists, self.served())) as archive:
            names = sorted(archive.namelist())

        self.assertEqual(len(names), 2, names)
        self.assertTrue(any("-RAR-" in n for n in names), names)
        self.assertFalse(any(n.endswith(".new") for n in names),
                         "the archive carries the temporary names the build used")

    def test_txt_serves_a_plain_text_file(self):
        self.build("txt")

        self.assertTrue(self.served().endswith(".txt"), self.lists_dir())

    def test_rar_serves_a_real_rar_archive(self):
        if not platform_compat.rar_command(getattr(config, "RAR_BINARY", None)):
            self.skipTest("no rar binary on this machine")
        self.build("rar")

        served = self.served()
        self.assertTrue(served.endswith(".rar"), self.lists_dir())
        with open(os.path.join(self.tree.lists, served), "rb") as handle:
            self.assertEqual(handle.read(4), b"Rar!", "not actually a rar archive")

    def test_the_rar_carries_the_published_names_not_the_temporary_ones(self):
        """rar has no equivalent of zipfile's arcname, so the members are
        staged under their final names first. Without that the archive would
        hold "DCCoreTest-2026-08-30.txt.new" and every reader would see the
        name that one build happened to use."""
        rar_bin = platform_compat.rar_command(getattr(config, "RAR_BINARY", None))
        if not rar_bin:
            self.skipTest("no rar binary on this machine")
        self.build("rar")

        listing = subprocess.run([rar_bin, "lb", os.path.join(self.tree.lists, self.served())],
                                 capture_output=True, text=True, timeout=120)
        members = [line.strip() for line in listing.stdout.splitlines() if line.strip()]

        self.assertEqual(len(members), 2, members)
        self.assertFalse(any(m.endswith(".new") for m in members), members)
        self.assertTrue(all(m.startswith(config.LIST_BASE_NAME) for m in members), members)


class TheTextListIsNotTheIndex(ListFormatCase):
    """The one that would corrupt something rather than merely fail.

    The master index is what @find searches and what the advert counts. The
    delivered .txt is that file with the album rows appended - so if the index
    scan ever picked it up, a search for "metallica" would offer an album row
    as though it were a track, and the advert would announce the albums as
    files.
    """

    def test_the_index_is_still_the_plain_list(self):
        self.build("txt")

        index = os.path.basename(list_mod.find_latest_list())

        self.assertNotIn(list_mod.FULL_LIST_MARKER, index)
        self.assertNotIn("-RAR-", index)

    def test_the_delivered_text_list_is_a_separate_file(self):
        self.build("txt")

        self.assertNotEqual(self.served(), os.path.basename(list_mod.find_latest_list()))

    def test_the_file_count_does_not_include_the_album_rows(self):
        """The number the advert publishes. Reading the delivered list instead
        of the index would inflate it by one row per album."""
        self.build("txt")

        count, _date, _size, _raw = list_mod.get_file_count_date_size_and_raw_bytes()

        self.assertEqual(count, len(self.tree.tracks))


class ItIsPackagingNotLessContent(ListFormatCase):

    def read_served(self):
        with io.open(os.path.join(self.tree.lists, self.served()), encoding="utf-8") as handle:
            return handle.read()

    def test_the_text_list_carries_the_album_rows_too(self):
        """A .txt can only be one file where the .zip is two. The album
        section is appended rather than dropped: choosing a format is a
        statement about packaging, not a request to hand out less."""
        self.build("txt")
        body = self.read_served()

        self.assertIn(f"!{config.NICKNAME} !rar", body)
        self.assertIn("Black Album", body)

    def test_the_two_sections_are_separated(self):
        """One file, two lists, and a person scrolling it has only the layout
        to tell them apart - where the .zip had two filenames to do it."""
        self.build("txt")
        lines = self.read_served().splitlines()
        header = next(i for i, line in enumerate(lines) if "Entire Album Folders" in line)

        self.assertEqual(lines[header - 1].strip(), "",
                         "the album list starts straight after the last file row")

    def test_and_carries_the_file_rows(self):
        self.build("txt")

        self.assertIn("Enter Sandman", self.read_served())

    def test_with_folder_packing_off_it_carries_only_the_file_rows(self):
        """#153's rule survives the new format: a bot that refuses !rar must
        not hand out a list of !rar instructions."""
        self.build("txt", RAR_ENABLED=False)

        self.assertNotIn("!rar", self.read_served())
        self.assertIn("Enter Sandman", self.read_served())


class TheChoiceIsNotTiedToFolderPacking(ListFormatCase):
    """RAR_ENABLED governs whether a stranger can make this bot pack an album
    folder on demand. Packing two text files once per rebuild, on the
    operator's own schedule, is a different job - so all three formats stay
    available either way."""

    def test_a_rar_list_is_built_even_with_folder_packing_off(self):
        if not platform_compat.rar_command(getattr(config, "RAR_BINARY", None)):
            self.skipTest("no rar binary on this machine")

        self.build("rar", RAR_ENABLED=False)

        self.assertTrue(self.served().endswith(".rar"), self.lists_dir())

    def test_and_that_rar_holds_only_the_file_list(self):
        """Both rules at once: the format is free, and the album list is still
        not shipped by a bot that refuses album requests."""
        rar_bin = platform_compat.rar_command(getattr(config, "RAR_BINARY", None))
        if not rar_bin:
            self.skipTest("no rar binary on this machine")

        self.build("rar", RAR_ENABLED=False)

        listing = subprocess.run([rar_bin, "lb", os.path.join(self.tree.lists, self.served())],
                                 capture_output=True, text=True, timeout=120)
        members = [line.strip() for line in listing.stdout.splitlines() if line.strip()]

        self.assertEqual(len(members), 1, members)
        self.assertNotIn("-RAR-", members[0])

    def test_the_settings_page_offers_all_three(self):
        import webserver
        fields = [f for category in webserver.build_settings_payload()["categories"]
                  for f in category["fields"]]
        field = [f for f in fields if f["name"] == "LIST_FORMAT"]

        self.assertEqual(len(field), 1, "LIST_FORMAT is not on the settings page")
        self.assertEqual(sorted(field[0]["choices"]), ["rar", "txt", "zip"])


class AFormatItCannotBuild(ListFormatCase):
    """The list is the whole point of the bot. Refusing to publish one over a
    packaging preference would take it off the air, so a rar that cannot be
    packed falls back - loudly - and the operator still has something to
    serve while they sort the binary out."""

    def setUp(self):
        super().setUp()
        self.real_rar_command = platform_compat.rar_command
        platform_compat.rar_command = lambda *args, **kwargs: None
        self.addCleanup(setattr, platform_compat, "rar_command", self.real_rar_command)

    def test_a_missing_rar_binary_publishes_a_zip_instead(self):
        self.build("rar")

        self.assertTrue(self.served().endswith(".zip"), self.lists_dir())

    def test_the_build_still_succeeds(self):
        """build() asserts this, but it is the actual point: the previous
        behaviour of a failed pack was a failed build, and a failed build is a
        bot with no list."""
        self.build("rar")

        self.assertTrue(list_mod.find_latest_list_file())

    def test_it_says_why_rather_than_quietly_downgrading(self):
        output = self.build("rar")

        self.assertIn("no rar binary was found", output)
        self.assertIn("RAR_BINARY", output, "the message does not say what to set")

    def test_a_staging_directory_a_killed_run_left_behind_is_cleaned_up(self):
        """rar packs from a scratch directory inside lists/. Nothing else in
        there ever looks at directories, so one left by a daemon killed
        mid-pack would sit until somebody noticed it."""
        stale = os.path.join(self.tree.lists, update_list._STAGING_PREFIX + "old")
        os.makedirs(stale)
        update_list._discard_stale_staging()

        self.assertFalse(os.path.exists(stale))

    def test_no_empty_rar_is_left_behind(self):
        """An empty or half-written .rar in lists/ would be served by the
        fallback in find_latest_list_file() as though it were a real list."""
        self.build("rar")

        self.assertEqual(self.artifacts(".rar"), [])


class ChangingTheFormat(ListFormatCase):

    def test_the_previous_format_is_not_left_behind(self):
        """Not just clutter: find_latest_list_file() falls back to whatever
        HAS been built, so a stale .zip beside a fresh .rar would go on being
        handed out the first time a build failed after the switch."""
        self.build("zip")
        self.assertEqual(len(self.artifacts(".zip")), 1, "setup did not build one")

        self.build("txt")

        self.assertEqual(self.artifacts(".zip"), [], self.lists_dir())

    def test_a_stale_rar_list_is_cleaned_up_like_any_other(self):
        """.rar had to be added to the extensions the prune is allowed to
        touch. Without it, a bot that once served .rar lists would keep every
        one of them for ever, and the newest would go on being served the
        first time a build failed after switching away."""
        stale = os.path.join(self.tree.lists, f"{config.LIST_BASE_NAME}-2020-01-01.rar")
        with io.open(stale, "w", encoding="utf-8") as handle:
            handle.write("last week's archive")

        self.build("zip")

        self.assertEqual(self.artifacts(".rar"), [], self.lists_dir())

    def test_before_the_next_rebuild_the_old_list_is_still_served(self):
        """The operator changes the setting at 14:00 and the weekly rebuild is
        on Sunday. Answering "list missing" until then would be a far bigger
        failure than handing somebody a .zip when the setting says .rar."""
        self.build("zip")

        self.set_config(LIST_FORMAT="rar")

        self.assertTrue(self.served().endswith(".zip"), self.lists_dir())

    def test_and_says_so_so_the_operator_is_not_left_wondering(self):
        self.build("zip")
        self.set_config(LIST_FORMAT="txt")

        buffer = io.StringIO()
        from contextlib import redirect_stdout
        with redirect_stdout(buffer):
            list_mod.find_latest_list_file()

        self.assertIn("No .txt list has been built yet", buffer.getvalue())

    def test_with_no_list_at_all_it_still_answers_nothing(self):
        self.assertIsNone(list_mod.find_latest_list_file())


class WhichFilesCountAsTheList(ListFormatCase):
    """dcc.py routes a request to the lists directory or the music directory
    on this answer alone."""

    def test_each_format_is_recognised(self):
        for name in ("DCCoreTest-2026-08-30.zip", "DCCoreTest-2026-08-30.rar",
                     "DCCoreTest" + list_mod.FULL_LIST_MARKER + "2026-08-30.txt"):
            with self.subTest(name=name):
                self.assertTrue(list_mod.is_list_artifact_name(name))

    def test_a_shared_rar_from_the_library_is_not_the_list(self):
        """The reason the match is on the name the builder writes rather than
        on the extension plus the base name appearing anywhere: people share
        .rar files out of their library, and this one would have been looked
        for among the lists and never found."""
        self.assertFalse(list_mod.is_list_artifact_name("Someone - DCCoreTest Sessions.rar"))

    def test_the_master_index_is_not_something_to_hand_out(self):
        """It is served through the delivered .txt, which has the album rows
        appended. Offering the index directly would be a second, quietly
        different answer to "@<nick>"."""
        self.assertFalse(list_mod.is_list_artifact_name("DCCoreTest-2026-08-30.txt"))

    def test_nor_is_the_album_list(self):
        self.assertFalse(list_mod.is_list_artifact_name("DCCoreTest-RAR-2026-08-30.txt"))

    def test_a_track_is_not(self):
        self.assertFalse(list_mod.is_list_artifact_name("Metallica/Black Album/01 - Enter Sandman.flac"))


class AValueThatIsNotOneOfTheThree(ListFormatCase):

    def test_the_settings_save_refuses_it_with_the_reason(self):
        with self.assertRaises(settings_file.SettingsWriteError) as caught:
            settings_file._check_writable("LIST_FORMAT", "tar", vars(config),
                                          settings_file.declared_types(vars(config)))

        self.assertIn("txt", str(caught.exception))

    def test_case_is_not_part_of_the_choice(self):
        """An operator typing "ZIP" means "zip". Refusing that would be
        pedantry; storing it would be a bot that serves no list."""
        types = settings_file.declared_types(vars(config))
        text, value = settings_file._check_writable("LIST_FORMAT", " RAR ", vars(config), types)

        self.assertEqual(value, "rar")
        self.assertEqual(text, "rar")

    def test_a_hand_edited_settings_file_keeps_the_default(self):
        path = os.path.join(self.tree.root, "settings.conf")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write("LIST_FORMAT = tarball\n")
        namespace = {"LIST_FORMAT": "zip", "__annotations__": {"LIST_FORMAT": str}}

        report = settings_file.apply_to(namespace, path=path, log=lambda *a: None)

        self.assertEqual(namespace["LIST_FORMAT"], "zip")
        self.assertEqual([name for name, _why in report["bad"]], ["LIST_FORMAT"])

    def test_one_that_reached_config_anyway_still_serves_a_list(self):
        """admin_config.py assigns straight onto config and answers to nobody,
        so the write-time check above is not the last line of defence."""
        self.build("zip")
        self.set_config(LIST_FORMAT="Zip Or Something")

        self.assertEqual(list_mod.list_format(), "zip")
        self.assertTrue(self.served().endswith(".zip"))


class WhatTheHelpNoticeSays(ListFormatCase):
    """#154's rule, applied to the new setting: never tell somebody to expect
    something this bot will not do."""

    def notice(self):
        import commands
        self.oserve.queued.clear()
        commands.handle_help_request(None, "dave", "#dccore-test")
        return " ".join(message for _user, message, _vip in self.oserve.queued)

    def test_it_names_the_format_that_will_actually_arrive(self):
        for fmt in ("txt", "zip", "rar"):
            with self.subTest(fmt=fmt):
                self.set_config(LIST_FORMAT=fmt)

                self.assertIn(fmt, self.notice())

    def test_it_does_not_promise_a_zip_when_it_will_send_a_rar(self):
        self.set_config(LIST_FORMAT="rar")

        self.assertNotIn("zip", self.notice())


if __name__ == "__main__":
    unittest.main()
