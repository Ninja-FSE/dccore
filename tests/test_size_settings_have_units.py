"""Sizes are read and typed in MB or KB, not in bytes.

Asked for after looking at the Settings page:

    Largest folder !rar will pack (bytes, 0 = no limit)   10737418240

Counting the zeros to check that says ten gigabytes is not work anybody
should be doing, and these span 8 KB to 10 GB.

STORED IN BYTES, UNCHANGED. settings.conf, admin_config.py and every reader in
the daemon keep the number they have always had - nothing migrates, and an
operator who edits the file by hand sees exactly what they saw before. Only
the dashboard divides, and only for display.

The unit is fixed per setting rather than chosen from the magnitude. A field
that switched unit as its value grew would move under an operator mid-edit,
and `0` - which several of these use to mean "no limit" - has no magnitude to
read at all. KB is used where MB would print 0.0078.
"""

import io
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import defaults as config  # noqa: E402
import webserver  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402

MB = 1024 * 1024
KB = 1024


class EverySizeSettingCarriesAUnit(DCCoreTestCase):

    def fields(self):
        found = {}
        for category in webserver.build_settings_payload()["categories"]:
            for field in category["fields"]:
                found[field["name"]] = field
        return found

    def test_the_large_sizes_are_in_megabytes(self):
        fields = self.fields()
        for name in ("MAX_RAR_FOLDER_SIZE", "MAX_FETCH_FILE_SIZE",
                     "MAX_LIST_TEXT_SIZE", "MAX_FETCH_FOLDER_FILE_SIZE",
                     "MAX_FETCH_LIST_FILE_SIZE"):
            with self.subTest(setting=name):
                self.assertEqual(fields[name]["unit"], "MB")
                self.assertEqual(fields[name]["unit_factor"], MB)

    def test_the_small_ones_are_in_kilobytes(self):
        """MB would print 0.0078 for an 8 KB banner limit, which is less
        readable than the bytes it replaced."""
        fields = self.fields()
        for name in ("DCC_SEND_BUFFER", "LIST_HEADER_MAX_BYTES"):
            with self.subTest(setting=name):
                self.assertEqual(fields[name]["unit"], "KB")
                self.assertEqual(fields[name]["unit_factor"], KB)

    def test_the_defaults_land_on_whole_units(self):
        """Not required, but true of every shipped value - and a default that
        displayed as 199.99 would be the first sign the factor is wrong."""
        fields = self.fields()
        for name, field in fields.items():
            if not field.get("unit"):
                continue
            with self.subTest(setting=name):
                self.assertEqual(field["value"] % field["unit_factor"], 0,
                                 f"{name} = {field['value']} is not a whole "
                                 f"{field['unit']}")

    def test_no_size_setting_was_left_in_bytes(self):
        """The sweep the request asked for - "and if any other such option".
        A settings label that still says bytes is one that was missed."""
        leftover = [name for name, label in webserver.SETTINGS_LABELS.items()
                    if "byte" in label.lower()
                    and name not in webserver.SETTINGS_UNITS
                    and not name.endswith("_FILE")]

        self.assertEqual(leftover, [],
                         "these still ask an operator for a byte count")

    def test_a_path_setting_did_not_get_a_unit(self):
        """LIST_RAWBYTES_FILE has "bytes" in its name and is a file path. A
        sweep by name rather than by meaning would have given it a factor."""
        self.assertNotIn("LIST_RAWBYTES_FILE", webserver.SETTINGS_UNITS)

    def test_the_stored_value_is_still_bytes(self):
        """The whole point: nothing migrates. The payload carries the byte
        count and the page divides it."""
        self.assertEqual(self.fields()["MAX_FETCH_FILE_SIZE"]["value"],
                         config.MAX_FETCH_FILE_SIZE)


class ThePacketSizeMenuReadsInKilobytes(DCCoreTestCase):
    """DCC_BLOCK_SIZE is a menu, not a number to type, so it keeps its byte
    values as the stored choice and gains readable text beside each one."""

    def field(self):
        for category in webserver.build_settings_payload()["categories"]:
            for f in category["fields"]:
                if f["name"] == "DCC_BLOCK_SIZE":
                    return f
        self.fail("DCC_BLOCK_SIZE is not on the settings page")

    def test_each_choice_reads_as_kilobytes(self):
        field = self.field()

        self.assertEqual(field["choice_labels"],
                         ["4 KB", "8 KB", "16 KB", "32 KB", "64 KB", "128 KB"])

    def test_the_values_are_still_the_byte_counts(self):
        """The label is what changes; the value saved is untouched."""
        field = self.field()

        self.assertEqual([str(c) for c in field["choices"]],
                         ["4096", "8192", "16384", "32768", "65536", "131072"])

    def test_there_is_one_label_per_choice(self):
        """A short list would silently pair the wrong text with a value."""
        field = self.field()

        self.assertEqual(len(field["choice_labels"]), len(field["choices"]))

    def test_it_has_no_unit_of_its_own(self):
        """A menu is not typed into, so a unit chip beside it would be noise -
        and dividing a select's value would break the option matching."""
        self.assertNotIn("unit", self.field())


class ThePageConvertsAtBothEnds(unittest.TestCase):
    """The conversion lives in two functions in app.js and nowhere else, so
    the baseline, the dirty set and the POST body all stay in bytes."""

    def source(self):
        with io.open(os.path.join(REPO_ROOT, "web", "app.js"),
                     encoding="utf-8") as handle:
            return handle.read()

    def test_both_directions_exist(self):
        source = self.source()

        self.assertIn("function bytesToUnit(", source)
        self.assertIn("function unitToBytes(", source)

    def test_display_divides_and_input_multiplies(self):
        source = self.source()

        self.assertIn("bytesToUnit(stored, field.unit_factor)", source)
        self.assertIn("unitToBytes(newValue, field.unit_factor)", source)

    def test_a_half_typed_box_is_not_read_as_zero(self):
        """0 means "no limit" for several of these, so an empty field must not
        convert into one."""
        source = self.source()
        body = source.split("function unitToBytes(", 1)[1][:500]

        self.assertIn("isFinite", body)

    def test_the_menu_labels_are_rendered_as_text_not_values(self):
        source = self.source()

        self.assertIn("field.choice_labels", source)
        self.assertIn('<option value="', source)


if __name__ == "__main__":
    unittest.main()
