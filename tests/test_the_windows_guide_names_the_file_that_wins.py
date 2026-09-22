"""WINDOWS.md sent the operator to a file whose value does not count (audit
M35, #637).

Its fix for `[WEBUI] Disabled via config.WEBUI_ENABLED = False.` was "set
WEBUI_ENABLED = True in admin_config.py or settings.conf". On a
configure-made install that declined the dashboard, settings.conf holds
`WEBUI_ENABLED = false` - written by the setup - and settings.conf is applied
AFTER admin_config.py, so a True added to admin_config.py changes nothing;
the operator restarts, sees the same line, and concludes the bot is broken.
(Before #623 the setup also seeded admin_config.py from a sample that already
said True, which made the doc's first option not just useless but already
the case.)

The guide now names settings.conf, says it is the file that wins where both
set a name, and says the setup writes the answer there. The audit found this
by reading the guide, so this reads it too.
"""

import io
import os
import re
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class TheFixForADisabledDashboard(unittest.TestCase):

    def passage(self):
        with io.open(os.path.join(REPO_ROOT, "docs", "WINDOWS.md"), encoding="utf-8") as handle:
            guide = handle.read()
        marker = "[WEBUI] Disabled via config.WEBUI_ENABLED = False."
        self.assertIn(marker, guide, "the log line the passage explains")
        after = guide.split(marker, 1)[1]
        # Up to the next bold heading of the same troubleshooting list.
        return after.split("\n**", 1)[0]

    def test_it_names_settings_conf_as_the_place(self):
        passage = self.passage()
        first_file = re.search(r"`(settings\.conf|admin_config\.py)`", passage)

        self.assertIsNotNone(first_file, passage)
        self.assertEqual(first_file.group(1), "settings.conf")

    def test_it_says_which_file_wins(self):
        passage = self.passage()

        self.assertIn("`settings.conf` wins", passage)
        self.assertIn("changes nothing", passage)

    def test_it_says_the_setup_wrote_the_answer_there(self):
        passage = self.passage()

        self.assertIn("configure.py", passage)
        self.assertIn("`false` if you declined", passage)

    def test_the_value_is_spelt_the_way_settings_conf_reads_it(self):
        """settings.conf is plain text: `true`, not Python's `True`. A copied
        `WEBUI_ENABLED = True` is accepted too, but the guide should show
        the file's own spelling."""
        self.assertIn("`WEBUI_ENABLED = true`", self.passage())


if __name__ == "__main__":
    unittest.main()
