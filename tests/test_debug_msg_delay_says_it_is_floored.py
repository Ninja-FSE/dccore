"""DEBUG_MSG_DELAY shipped at 0.5 s and was described as the debug channel's
own pace, while the pacer floors it to MSG_DELAY (audit M48, #650).

The debug drain asks the shared clock for max(MSG_DELAY, DEBUG_MSG_DELAY) -
on purpose, since #406: the two lanes used to sleep on unrelated clocks and
the server saw their sum. So any DEBUG_MSG_DELAY below MSG_DELAY is inert,
and the shipped 0.5 was one. Four operator-facing texts (the defaults.py
comment, the Settings-page help in three languages, settings.conf.sample)
said "the same wait, for lines going to your debug channel", and an
operator lowering it to speed the debug channel up saw nothing change and
had no way to learn why.

The pacer is unchanged. The default is now 0, meaning "the same as
MSG_DELAY", and every text says the floor out loud.
"""

import io
import json
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import runtime  # noqa: E402
import settings_help  # noqa: E402

from tests.support import DCCoreTestCase  # noqa: E402


def read(*parts):
    with io.open(os.path.join(REPO_ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


class TheDefaultDoesNotPromiseASpeed(unittest.TestCase):

    def shipped(self, name):
        """The value defaults.py assigns, read from the file - the module's
        attribute may have been set by whatever test ran before this one."""
        line = [l for l in read("defaults.py").splitlines() if l.startswith(name + ":")][0]
        return float(line.split("=", 1)[1].split("#", 1)[0])

    def test_it_ships_as_the_same_as_msg_delay(self):
        self.assertEqual(self.shipped("DEBUG_MSG_DELAY"), 0)

    def test_which_the_pacer_reads_as_msg_delay(self):
        """The expression the drain uses, with the shipped values."""
        self.assertEqual(max(self.shipped("MSG_DELAY"), self.shipped("DEBUG_MSG_DELAY")),
                         self.shipped("MSG_DELAY"))


class EveryTextSaysTheFloor(unittest.TestCase):

    def test_the_settings_page_help_in_english(self):
        help_text = settings_help.PLAIN_HELP["DEBUG_MSG_DELAY"]

        self.assertIn("Never less than MSG_DELAY", help_text)
        self.assertIn("0 means the same as MSG_DELAY", help_text)

    def test_and_in_french_and_spanish(self):
        for lang, must in (("fr", "Jamais moins que MSG_DELAY"), ("es", "Nunca menos que MSG_DELAY")):
            with self.subTest(lang=lang):
                strings = json.loads(read("web", "lang", lang + ".json"))
                self.assertIn(must, strings["settings.field.DEBUG_MSG_DELAY.help"])

    def test_the_sample_file(self):
        sample = read("settings.conf.sample")
        block = sample.split("#DEBUG_MSG_DELAY", 1)[0][-700:]

        self.assertIn("Never less than", block)
        self.assertIn("MSG_DELAY", block)
        self.assertIn("#DEBUG_MSG_DELAY = 0.0\n", sample)

    def test_the_defaults_comment(self):
        line = [l for l in read("defaults.py").splitlines() if l.startswith("DEBUG_MSG_DELAY")][0]

        self.assertIn("MSG_DELAY", line.split("#", 1)[1])
        self.assertIn("0 = the same as MSG_DELAY", line)


class TheDrainStillFloorsIt(DCCoreTestCase):
    """The behaviour the texts now describe, at the pacer: a value below
    MSG_DELAY asks the clock for MSG_DELAY. Read through the same call the
    drain makes, with a spy in place of the clock."""

    def setUp(self):
        super().setUp()
        self.asked = []
        real = runtime.outbound_pacer
        runtime.outbound_pacer = type("Spy", (), {"wait_for_slot": lambda _s, n: self.asked.append(n)})()
        self.addCleanup(setattr, runtime, "outbound_pacer", real)
        self.set_config(MSG_DELAY=5.0)

    def ask_as_the_drain_does(self):
        import defaults as config
        runtime.outbound_pacer.wait_for_slot(
            max(config.MSG_DELAY, getattr(config, 'DEBUG_MSG_DELAY', 0)))

    def test_zero_is_msg_delay(self):
        self.set_config(DEBUG_MSG_DELAY=0)
        self.ask_as_the_drain_does()
        self.assertEqual(self.asked, [5.0])

    def test_below_msg_delay_is_msg_delay(self):
        self.set_config(DEBUG_MSG_DELAY=0.5)
        self.ask_as_the_drain_does()
        self.assertEqual(self.asked, [5.0])

    def test_above_msg_delay_is_itself(self):
        self.set_config(DEBUG_MSG_DELAY=8.0)
        self.ask_as_the_drain_does()
        self.assertEqual(self.asked, [8.0])

    def test_the_drain_really_uses_that_expression(self):
        source = read("announce.py")
        self.assertIn("max(config.MSG_DELAY, getattr(config, 'DEBUG_MSG_DELAY', 0))", source)


if __name__ == "__main__":
    unittest.main()
