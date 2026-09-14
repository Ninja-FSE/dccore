"""Two faults an operator meets on their first day, both of them silent.

  * #446 a `settings.conf` saved with a byte-order mark lost its FIRST
    setting. Three invisible bytes, written by Notepad as a matter of course,
    and the daemon reads a file that plainly sets a value while ignoring it.
  * #447 the pre-flight check reported the admin console "enabled" on a fresh
    install where it was off - `ADMIN_HOSTMASKS = [""]` is a truthy list of
    one, so counting it said one pattern while the daemon accepted none.

A check that reports a feature as enabled when it is disabled is worse than
one that says nothing: the operator stops looking.
"""

import io
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import adminchat  # noqa: E402
import defaults as config  # noqa: E402
import settings_file  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


class AByteOrderMarkDoesNotEatTheFirstSetting(DCCoreTestCase):

    def apply(self, text):
        folder = tempfile.mkdtemp()
        path = os.path.join(folder, "settings.conf")
        with io.open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return settings_file.apply_to(vars(config), path=path,
                                      log=lambda *a, **k: None)

    def test_the_first_setting_survives_a_bom(self):
        report = self.apply("﻿MSG_DELAY = 7.5\nMAX_DCC_SLOTS = 9\n")

        self.assertIn("MSG_DELAY", report["applied"])

    def test_it_is_not_merely_reported_as_unknown(self):
        """The failure mode: accepted as a setting nobody has heard of, so
        nothing raises and the default silently stands."""
        report = self.apply("﻿MSG_DELAY = 7.5\n")

        self.assertEqual(report["unknown"], [])

    def test_a_file_without_a_bom_is_unchanged(self):
        """utf-8-sig is identical to utf-8 when there is no mark, and this
        says so rather than assuming it."""
        report = self.apply("MSG_DELAY = 7.5\nMAX_DCC_SLOTS = 9\n")

        self.assertIn("MSG_DELAY", report["applied"])
        self.assertIn("MAX_DCC_SLOTS", report["applied"])
        self.assertEqual(report["unknown"], [])

    def test_the_save_side_reads_it_the_same_way(self):
        """save() decodes the same file to edit it in place. Left as plain
        utf-8 there, a mark would reappear inside the first line of the
        rewritten file - so the bug would come back on the next save."""
        source = io.open(os.path.join(REPO_ROOT, "settings_file.py"),
                         encoding="utf-8").read()

        self.assertNotIn('decode("utf-8")', source,
                         "the save side still decodes without utf-8-sig")


class ThePreflightReportsWhatTheDaemonAccepts(unittest.TestCase):

    def setUp(self):
        self._real = getattr(config, "ADMIN_HOSTMASKS", [])
        self.addCleanup(setattr, config, "ADMIN_HOSTMASKS", self._real)

    def patterns_for(self, masks):
        config.ADMIN_HOSTMASKS = masks
        return adminchat.admin_host_patterns()

    def test_an_empty_string_mask_is_no_pattern_at_all(self):
        """The premise. ADMIN_HOSTMASKS = [""] is a truthy list of one, which
        is why counting the list reported a console that does not exist."""
        self.assertEqual(self.patterns_for([""]), [])
        self.assertEqual(self.patterns_for(["   "]), [])

    def test_a_real_mask_still_yields_a_pattern(self):
        self.assertEqual(len(self.patterns_for(["*!*@example.com"])), 1)

    def test_the_check_counts_patterns_rather_than_masks(self):
        source = io.open(os.path.join(REPO_ROOT, "scripts", "setup_check.py"),
                         encoding="utf-8").read()
        block = source.split("Admin console", 1)[1].split("verdict", 1)[0]

        self.assertIn("admin_host_patterns()", block,
                      "the check counts the raw list again, so [''] reads as "
                      "one enabled pattern")
        self.assertNotIn("len(masks)", block)

    def test_the_check_cannot_be_what_breaks_the_check(self):
        """adminchat is imported for this; if that import fails the pre-flight
        must still produce a verdict rather than a traceback."""
        source = io.open(os.path.join(REPO_ROOT, "scripts", "setup_check.py"),
                         encoding="utf-8").read()
        block = source.split("Admin console", 1)[1].split("verdict", 1)[0]

        self.assertIn("except Exception", block)
