"""Windows device names (CON, NUL, COM1 ...) in anything DCCore turns into a path (#1208).

Windows refuses to create a file or folder called CON, PRN, AUX, NUL, COM1-9 or
LPT1-9, with any extension and in any case. These tests run on every platform:
the helper is the same everywhere on purpose, so a list directory or archive
made on Linux still opens on the Windows machine it is copied to.
"""

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import dcc  # noqa: E402
import dcc_fetch  # noqa: E402
import library  # noqa: E402
import list as list_mod  # noqa: E402
import list_fetch  # noqa: E402
import platform_compat  # noqa: E402

RESERVED = (["CON", "PRN", "AUX", "NUL"]
            + [f"COM{n}" for n in range(1, 10)]
            + [f"LPT{n}" for n in range(1, 10)])


class TheHelperTests(unittest.TestCase):

    def test_every_reserved_name_comes_out_safe(self):
        for name in RESERVED:
            for shape in (name, name.lower(), name.title(), f"{name}.txt",
                          f"{name.lower()}.tar.gz", f"{name} "):
                with self.subTest(shape=shape):
                    self.assertTrue(platform_compat.is_windows_reserved(shape))
                    safe = platform_compat.windows_safe_name(shape)
                    self.assertFalse(platform_compat.is_windows_reserved(safe), safe)
                    self.assertTrue(safe.startswith(shape.strip()[:3]), safe)

    def test_the_underscore_goes_after_the_base_and_before_the_extension(self):
        self.assertEqual(platform_compat.windows_safe_name("CON"), "CON_")
        self.assertEqual(platform_compat.windows_safe_name("aux.txt"), "aux_.txt")
        self.assertEqual(platform_compat.windows_safe_name("NUL.tar.gz"), "NUL_.tar.gz")
        self.assertEqual(platform_compat.windows_safe_name("COM1 .txt"), "COM1_.txt")

    def test_the_superscript_digits_are_reserved_too(self):
        self.assertTrue(platform_compat.is_windows_reserved("COM¹"))
        self.assertTrue(platform_compat.is_windows_reserved("lpt³.log"))

    def test_ordinary_names_are_left_alone(self):
        for name in ("CONCERT", "Console", "Auxiliary", "NULL", "COM10", "COM0",
                     "LPT", "Aux-Cord", "Music", "Greatest Hits", "A.CON", "x.NUL"):
            with self.subTest(name=name):
                self.assertFalse(platform_compat.is_windows_reserved(name))
                self.assertEqual(platform_compat.windows_safe_name(name), name)

    def test_trailing_dots_and_spaces_are_trimmed_unless_asked_not_to(self):
        self.assertEqual(platform_compat.windows_safe_name("Live. . "), "Live")
        self.assertEqual(platform_compat.windows_safe_name("CON."), "CON_")
        self.assertEqual(platform_compat.windows_safe_name("Live.", trim_end=False), "Live.")

    def test_nothing_is_not_an_error(self):
        self.assertEqual(platform_compat.windows_safe_name(""), "")
        self.assertEqual(platform_compat.windows_safe_name(None), "")
        self.assertFalse(platform_compat.is_windows_reserved(None))


class ListDirectoryTests(unittest.TestCase):

    def test_a_list_named_like_a_device_gets_a_directory_that_can_be_created(self):
        for name in RESERVED:
            slug = list_mod.list_slug(name)
            with self.subTest(name=name):
                self.assertFalse(platform_compat.is_windows_reserved(slug), slug)
                self.assertTrue(slug.startswith(name + "-"), slug)

    def test_it_cannot_collide_with_a_list_named_like_the_fix(self):
        """Not "CON_": a list may well be called that already."""
        self.assertNotEqual(list_mod.list_slug("CON"), list_mod.list_slug("CON_"))
        self.assertEqual(list_mod.list_slug("CON_"), "CON_")

    def test_the_slug_is_stable_and_other_names_are_unchanged(self):
        self.assertEqual(list_mod.list_slug("NUL"), list_mod.list_slug("NUL"))
        for name in ("Films", "CONCERT", "Console", "Music 2"):
            self.assertEqual(list_mod.list_slug(name), name)


class ListEditorTests(unittest.TestCase):

    @staticmethod
    def entry(name, primary=False):
        return library.ServedList(name, primary, [], [])

    def test_a_reserved_list_name_is_refused_and_named(self):
        for name in ("NUL", "con", "Com1", "aux.txt"):
            found = library.list_problems([self.entry("Music", primary=True), self.entry(name)])
            with self.subTest(name=name):
                self.assertTrue(any(repr(name) in line and "Windows" in line for line in found), found)

    def test_ordinary_names_are_accepted(self):
        for name in ("CONCERT", "Console", "Auxiliary"):
            found = library.list_problems([self.entry("Music", primary=True), self.entry(name)])
            with self.subTest(name=name):
                self.assertEqual(found, [])

    def test_save_refuses_it(self):
        with self.assertRaises(ValueError) as caught:
            library.save_lists([self.entry("NUL", primary=True)], path=os.devnull)
        self.assertIn("NUL", str(caught.exception))


class ArchiveNameTests(unittest.TestCase):

    def test_a_folder_named_like_a_device_is_offered_under_its_own_name(self):
        """AutoQ matches the received name with the folder's own: "AUX_.rar"
        would leave the request outstanding forever."""
        for leaf in RESERVED:
            with self.subTest(leaf=leaf):
                self.assertEqual(dcc._sanitize_rar_leaf_name(leaf) + ".rar", leaf + ".rar")

    def test_the_name_on_disk_is_safe_for_a_top_level_device_folder(self):
        old = dcc.config.FILE_DIRECTORY
        dcc.config.FILE_DIRECTORY = os.path.abspath("lib")
        self.addCleanup(setattr, dcc.config, "FILE_DIRECTORY", old)
        for leaf in ("AUX", "com1"):
            with self.subTest(leaf=leaf):
                name = dcc._rar_archive_disk_name(os.path.join("lib", leaf))
                self.assertFalse(platform_compat.is_windows_reserved(name), name)
                # A digest now, not the fixed "_" suffix (#1249 review) -
                # see test_it_cannot_collide_with_a_folder_named_like_the_fix
                # for why.
                self.assertTrue(name.startswith(leaf + "-"), name)
                self.assertTrue(name.endswith(".rar"), name)

    def test_it_cannot_collide_with_a_folder_named_like_the_fix(self):
        """#1249 review, confirmed: "AUX" and a folder already named "AUX_"
        both used to become "AUX_.rar" - windows_safe_name()'s own fixed
        suffix is not itself reserved, so the second path passed through
        untouched and landed on the exact name the first was given. `rar a`
        adds to an existing archive rather than replacing it, so the second
        requester silently received both albums packed together - the same
        #162 finding #7 this function exists to prevent, reached a
        different way."""
        old = dcc.config.FILE_DIRECTORY
        dcc.config.FILE_DIRECTORY = os.path.abspath("lib")
        self.addCleanup(setattr, dcc.config, "FILE_DIRECTORY", old)
        reserved = dcc._rar_archive_disk_name(os.path.join("lib", "AUX"))
        already_suffixed = dcc._rar_archive_disk_name(os.path.join("lib", "AUX_"))
        self.assertNotEqual(reserved, already_suffixed)
        self.assertEqual(already_suffixed, "AUX_.rar")

    def test_the_digest_is_stable_and_ordinary_names_are_unchanged(self):
        old = dcc.config.FILE_DIRECTORY
        dcc.config.FILE_DIRECTORY = os.path.abspath("lib")
        self.addCleanup(setattr, dcc.config, "FILE_DIRECTORY", old)
        first = dcc._rar_archive_disk_name(os.path.join("lib", "AUX"))
        second = dcc._rar_archive_disk_name(os.path.join("lib", "AUX"))
        self.assertEqual(first, second)
        self.assertEqual(dcc._rar_archive_disk_name(os.path.join("lib", "Greatest Hits")),
                         "Greatest_Hits.rar")

    def test_a_device_folder_below_another_keeps_the_name_it_had(self):
        """"Music_AUX.rar" was never a device name; nothing about it changes."""
        old = dcc.config.FILE_DIRECTORY
        dcc.config.FILE_DIRECTORY = os.path.abspath("lib")
        self.addCleanup(setattr, dcc.config, "FILE_DIRECTORY", old)
        self.assertEqual(dcc._rar_archive_disk_name(os.path.join("lib", "Music", "AUX")),
                         "Music_AUX.rar")

    def test_the_shown_name_and_the_queued_name_still_agree(self):
        """Both are _sanitize_rar_leaf_name(); the queue row's name is what the
        packer later compares against."""
        self.assertEqual(dcc._sanitize_rar_leaf_name("NUL"), "NUL")

    def test_ordinary_names_are_exactly_what_they_were(self):
        for leaf, expected in (("Greatest Hits", "Greatest_Hits"),
                               ("Album [WEB] [320K]", "Album_[WEB]_[320K]"),
                               ("Live.", "Live."),
                               ("CONCERT", "CONCERT")):
            self.assertEqual(dcc._sanitize_rar_leaf_name(leaf), expected)


class FetchedNameTests(unittest.TestCase):

    def test_a_fetched_file_offered_as_a_device_name_is_stored_under_a_safe_one(self):
        self.assertEqual(dcc_fetch._sanitize_offer_filename("NUL.txt"), "NUL_.txt")
        self.assertEqual(dcc_fetch._sanitize_offer_filename("album.zip"), "album.zip")

    def test_a_bot_called_like_a_device_gets_a_directory_that_can_be_created(self):
        for nick in ("Con", "aux", "COM1", "NUL"):
            with self.subTest(nick=nick):
                name = list_fetch._sanitize_bot_dir_name(nick)
                self.assertFalse(platform_compat.is_windows_reserved(name.lower()), name)

    def test_an_ordinary_nick_is_unchanged(self):
        self.assertEqual(list_fetch._sanitize_bot_dir_name("Console"), "Console")

    def test_it_cannot_collide_with_a_bot_named_like_the_fix(self):
        """#1249 review, confirmed: "AUX" and a bot literally named "AUX_"
        both used to come out as "AUX_" - windows_safe_name()'s own fixed
        suffix is not itself reserved, so the second nick passed through
        unchanged and landed in the exact directory the first was given,
        one bot's held lists overwriting the other's."""
        reserved = list_fetch._sanitize_bot_dir_name("AUX")
        already_suffixed = list_fetch._sanitize_bot_dir_name("AUX_")
        self.assertNotEqual(reserved, already_suffixed)
        self.assertEqual(already_suffixed, "AUX_")

    def test_the_digest_is_stable_and_case_insensitive(self):
        """Lower-cased before hashing, since every caller (list_extract_dir()
        and friends) lower-cases the result anyway - "AUX" and "Aux" are one
        nick on IRC and must still land on one directory, not two."""
        first = list_fetch._sanitize_bot_dir_name("AUX")
        second = list_fetch._sanitize_bot_dir_name("AUX")
        self.assertEqual(first, second)
        self.assertEqual(list_fetch._sanitize_bot_dir_name("AUX").lower(),
                         list_fetch._sanitize_bot_dir_name("Aux").lower())


if __name__ == "__main__":
    unittest.main()
